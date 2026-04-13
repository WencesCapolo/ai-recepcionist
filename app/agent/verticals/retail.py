import json
import logging
from typing import Any

import logfire
from rapidfuzz import process, fuzz

from app.clients.models import ClientConfig, RetailToolConfig, PaymentConfig
from app.integrations.sheets import SheetsClient
from app.integrations.mercadopago import MercadoPagoClient, MercadoPagoError

logger = logging.getLogger(__name__)

MP_PAYMENT_TTL = 86400


def _format_price(precio: Any) -> str:
    try:
        return f"${float(precio):,.0f}"
    except (ValueError, TypeError):
        return str(precio)


def _parse_price(raw: str) -> float:
    cleaned = str(raw).strip().replace("$", "").replace(" ", "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    return float(cleaned)


def build_retail_tools(
    config: ClientConfig,
    *,
    sheets: SheetsClient,
    redis: Any = None,
    user_phone: str = "",
    **_: Any,
) -> list[dict]:
    cfg = config.tool_config.retail
    if cfg is None:
        return []

    tools = []
    enabled = frozenset(config.tools_enabled)

    if "get_price" in enabled:
        tools.append(_make_get_price(cfg, sheets, config))
    if "get_stock" in enabled:
        tools.append(_make_get_stock(cfg, sheets, config))
    if "get_all_products" in enabled:
        tools.append(_make_get_all_products(cfg, sheets, config))
    if "get_products_by_category" in enabled:
        tools.append(_make_get_products_by_category(cfg, sheets, config))
    if "generate_payment_link" in enabled and config.tool_config.payment and redis and user_phone:
        tools.append(_make_generate_payment_link(
            cfg=cfg,
            payment_cfg=config.tool_config.payment,
            sheets=sheets,
            redis=redis,
            user_phone=user_phone,
            client_id=str(config.id),
        ))

    return tools


def _make_get_price(cfg: RetailToolConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler(product: str) -> str:
        with logfire.span("tool.get_price", client_id=str(config.id)):
            col = cfg.columns
            rows = sheets.find_products(cfg.sheet_id, product, worksheet=cfg.tab)
            if not rows:
                return f"No encontré '{product}' en el catálogo."
            if len(rows) == 1:
                row = rows[0]
                return f"{row[col['product']]}: {_format_price(row[col['price']])} por {row[col['unit']]}."
            lines = [
                f"{r[col['product']]}: {_format_price(r[col['price']])} por {r[col['unit']]}"
                for r in rows
            ]
            return "\n".join(lines)

    return {
        "definition": {
            "name": "get_price",
            "description": "Consulta el precio de un producto. Si hay varias variantes, devuelve todos los precios.",
            "input_schema": {
                "type": "object",
                "properties": {"product": {"type": "string", "description": "Nombre o categoría del producto"}},
                "required": ["product"],
            },
        },
        "handler": handler,
    }


def _make_get_stock(cfg: RetailToolConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler(product: str) -> str:
        with logfire.span("tool.get_stock", client_id=str(config.id)):
            col = cfg.columns
            rows = sheets.find_products(cfg.sheet_id, product, worksheet=cfg.tab)
            if not rows:
                return f"No encontré '{product}' en el catálogo."
            if len(rows) == 1:
                row = rows[0]
                if int(row[col["stock"]]) == 0:
                    return f"{row[col['product']]}: sin stock por el momento."
                return f"Sí, tenemos {row[col['product']]}."
            in_stock     = [r for r in rows if int(r[col["stock"]]) > 0]
            out_of_stock = [r for r in rows if int(r[col["stock"]]) == 0]
            if not in_stock:
                return f"No tenemos {product} en stock en este momento."
            result = f"Sí, tenemos: {', '.join(r[col['product']] for r in in_stock)}."
            if out_of_stock:
                result += f" Sin stock: {', '.join(r[col['product']] for r in out_of_stock)}."
            return result

    return {
        "definition": {
            "name": "get_stock",
            "description": "Consulta si hay stock de un producto. Si hay varias variantes, devuelve todas.",
            "input_schema": {
                "type": "object",
                "properties": {"product": {"type": "string", "description": "Nombre o categoría del producto"}},
                "required": ["product"],
            },
        },
        "handler": handler,
    }


def _make_get_all_products(cfg: RetailToolConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler() -> str:
        with logfire.span("tool.get_all_products", client_id=str(config.id)):
            col = cfg.columns
            rows = sheets.get_all_rows(cfg.sheet_id, worksheet=cfg.tab)
            if not rows:
                return "No se pudo cargar el catálogo en este momento."
            by_category: dict[str, list[str]] = {}
            for row in rows:
                cat = row.get(col["category"], "General")
                by_category.setdefault(cat, []).append(
                    f"  • {row[col['product']]} — {_format_price(row[col['price']])} por {row[col['unit']]}"
                )
            lines = ["Estos son nuestros productos:\n"]
            for cat, products in by_category.items():
                lines.append(f"*{cat}*")
                lines.extend(products)
                lines.append("")
            return "\n".join(lines).strip()

    return {
        "definition": {
            "name": "get_all_products",
            "description": "Devuelve el catálogo completo de productos con precios, agrupado por categoría.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        "handler": handler,
    }


def _make_get_products_by_category(cfg: RetailToolConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler(category: str) -> str:
        with logfire.span("tool.get_products_by_category", client_id=str(config.id)):
            col = cfg.columns
            rows = sheets.get_all_rows(cfg.sheet_id, worksheet=cfg.tab)
            if not rows:
                return "No se pudo cargar el catálogo en este momento."
            all_categories = list(dict.fromkeys(
                str(r.get(col["category"], "")) for r in rows if r.get(col["category"])
            ))
            match = process.extractOne(category, all_categories, scorer=fuzz.partial_ratio, score_cutoff=60)
            if not match:
                return f"No encontré la categoría '{category}'. Las categorías disponibles son: {', '.join(all_categories)}."
            matched_category = match[0]
            matching_rows = [r for r in rows if r.get(col["category"], "") == matched_category]
            lines = [f"Productos en la categoría '{matched_category}':\n"]
            for row in matching_rows:
                lines.append(f"  • {row[col['product']]} — {_format_price(row[col['price']])} por {row[col['unit']]}")
            return "\n".join(lines).strip()

    return {
        "definition": {
            "name": "get_products_by_category",
            "description": "Busca productos por categoría. Devuelve la lista de productos.",
            "input_schema": {
                "type": "object",
                "properties": {"category": {"type": "string"}},
                "required": ["category"],
            },
        },
        "handler": handler,
    }


def _make_generate_payment_link(
    cfg: RetailToolConfig,
    payment_cfg: PaymentConfig,
    sheets: SheetsClient,
    redis: Any,
    user_phone: str,
    client_id: str,
) -> dict:
    mp = MercadoPagoClient(access_token=payment_cfg.access_token, sandbox=payment_cfg.sandbox)

    async def handler(items: list[dict], sucursal: str) -> str:
        with logfire.span("tool.generate_payment_link", client_id=client_id):
            col = cfg.columns
            mp_items: list[dict] = []
            resolved_lines: list[str] = []

            for item in items:
                product  = item.get("product", "")
                quantity = int(item.get("quantity", 1))
                rows = sheets.find_products(cfg.sheet_id, product, worksheet=cfg.tab, score_cutoff=85)
                if not rows:
                    rows = sheets.find_products(cfg.sheet_id, product, worksheet=cfg.tab)
                if not rows:
                    return f"No encontré el producto '{product}' en el catálogo."
                if len(rows) > 1:
                    options = "\n".join(f"- {r[col['product']]}" for r in rows)
                    return f"Encontré varias variantes de '{product}'. Especificá cuál querés:\n{options}"
                row = rows[0]
                try:
                    unit_price = _parse_price(str(row[col["price"]]))
                except ValueError as e:
                    logfire.error("tool.generate_payment_link.price_parse_error", client_id=client_id, error=str(e))
                    return "Hubo un problema con el precio de un producto. Llamá al local."
                mp_items.append({"title": row[col["product"]], "quantity": quantity, "unit_price": unit_price})
                resolved_lines.append(f"  • {quantity}x {row[col['product']]} — ${unit_price * quantity:,.0f} ARS")

            try:
                preference = await mp.create_payment_link(items=mp_items)
            except MercadoPagoError as e:
                logfire.error("tool.generate_payment_link.mp_error", client_id=client_id, error=str(e))
                return "No pude generar el link de pago en este momento. Intentá más tarde o llamá al local."

            grand_total = sum(i["unit_price"] * i["quantity"] for i in mp_items)
            redis.set(
                f"mp_payment:{preference.preference_id}",
                json.dumps({
                    "user_phone": user_phone,
                    "client_id": client_id,
                    "sucursal": sucursal,
                    "items": mp_items,
                    "total": grand_total,
                }),
                ex=MP_PAYMENT_TTL,
            )

            return (
                f"Acá tenés el link para pagar tu pedido en {sucursal}:\n"
                f"{chr(10).join(resolved_lines)}\n"
                f"*Total: ${grand_total:,.0f} ARS*\n\n"
                f"{preference.init_point}\n\n"
                "Cuando lo pagues, pasás a retirarlo en el local."
            )

    return {
        "definition": {
            "name": "generate_payment_link",
            "description": "Genera link de pago MercadoPago.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product":  {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1},
                            },
                            "required": ["product", "quantity"],
                        },
                    },
                    "sucursal": {"type": "string"},
                },
                "required": ["items", "sucursal"],
            },
        },
        "handler": handler,
    }
