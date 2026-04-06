import json
import logging
from typing import Any

from rapidfuzz import process, fuzz

from app.agent.base_toolset import ToolsetProvider
from app.clients.models import ClientConfig
from app.integrations.sheets import SheetsClient
from app.integrations.mercadopago import MercadoPagoClient, MercadoPagoError
from app.agent.registry import RETAIL_TOOLS

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


class RetailToolset:
    name = "retail"
    required_tools = frozenset(t.value for t in RETAIL_TOOLS)

    def is_applicable(self, config: ClientConfig) -> bool:
        enabled = frozenset(config.tools_enabled)
        return bool(enabled & self.required_tools)

    def build(self, config: ClientConfig, **deps: Any) -> list[dict]:
        sheets: SheetsClient = deps["sheets"]
        redis = deps.get("redis")
        user_phone = deps.get("user_phone", "")
        client_id = deps.get("client_id", "")

        all_tools = []
        if "get_price" in config.tools_enabled:
            all_tools.append(self._make_get_price(config, sheets))
        if "get_stock" in config.tools_enabled:
            all_tools.append(self._make_get_stock(config, sheets))
        if "get_all_products" in config.tools_enabled:
            all_tools.append(self._make_get_all_products(config, sheets))
        if "get_products_by_category" in config.tools_enabled:
            all_tools.append(self._make_get_products_by_category(config, sheets))
        if "get_hours" in config.tools_enabled:
            all_tools.append(self._make_get_hours(config))
        
        if "generate_payment_link" in config.tools_enabled and config.mp_access_token and redis and user_phone and config.sheet_id:
            all_tools.append(self._build_generate_payment_link(
                sheets=sheets,
                sheet_id=config.sheet_id,
                access_token=config.mp_access_token,
                sandbox=config.mp_sandbox,
                redis=redis,
                client_id=client_id,
                user_phone=user_phone,
            ))
        return all_tools

    def _make_get_price(self, config: ClientConfig, sheets: SheetsClient) -> dict:
        async def handler(product: str) -> str:
            if not config.sheet_id:
                return "No hay información de precios disponible."
            rows = sheets.find_products(config.sheet_id, product)
            if not rows:
                return f"No encontré '{product}' en el catálogo."
            if len(rows) == 1:
                row = rows[0]
                return f"{row['producto']}: {_format_price(row['precio'])} por {row['unidad']}."
            lines = [f"{r['producto']}: {_format_price(r['precio'])} por {r['unidad']}" for r in rows]
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

    def _make_get_stock(self, config: ClientConfig, sheets: SheetsClient) -> dict:
        async def handler(product: str) -> str:
            if not config.sheet_id:
                return "No hay información de stock disponible."
            rows = sheets.find_products(config.sheet_id, product)
            if not rows:
                return f"No encontré '{product}' en el catálogo."
            if len(rows) == 1:
                row = rows[0]
                if int(row["stock"]) == 0:
                    return f"{row['producto']}: sin stock por el momento."
                return f"Sí, tenemos {row['producto']}."
            in_stock    = [r for r in rows if int(r["stock"]) > 0]
            out_of_stock = [r for r in rows if int(r["stock"]) == 0]
            if not in_stock:
                return f"No tenemos {product} en stock en este momento."
            result = f"Sí, tenemos: {', '.join(r['producto'] for r in in_stock)}."
            if out_of_stock:
                result += f" Sin stock: {', '.join(r['producto'] for r in out_of_stock)}."
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

    def _make_get_all_products(self, config: ClientConfig, sheets: SheetsClient) -> dict:
        async def handler() -> str:
            if not config.sheet_id:
                return "No hay catálogo disponible."
            rows = sheets.get_all_rows(config.sheet_id)
            if not rows:
                return "No se pudo cargar el catálogo en este momento."
            by_category: dict[str, list[str]] = {}
            for row in rows:
                cat = row.get("categoria", "General")
                by_category.setdefault(cat, []).append(
                    f"  • {row['producto']} — {_format_price(row['precio'])} por {row['unidad']}"
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

    def _make_get_hours(self, config: ClientConfig) -> dict:
        async def handler() -> str:
            for line in config.system_prompt.splitlines():
                if line.strip().lower().startswith("horario"):
                    return line.strip()
            return "Consultá directamente con el local para conocer el horario."

        return {
            "definition": {
                "name": "get_hours",
                "description": "Devuelve el horario de atención del local.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            "handler": handler,
        }

    def _make_get_products_by_category(self, config: ClientConfig, sheets: SheetsClient) -> dict:
        async def handler(category: str) -> str:
            if not config.sheet_id:
                return "No hay catálogo disponible."
            rows = sheets.get_all_rows(config.sheet_id)
            if not rows:
                return "No se pudo cargar el catálogo en este momento."
            all_categories = list(dict.fromkeys(str(r.get("categoria", "")) for r in rows if r.get("categoria")))
            match = process.extractOne(category, all_categories, scorer=fuzz.partial_ratio, score_cutoff=60)
            if not match:
                return f"No encontré la categoría '{category}'. Las categorías disponibles son: {', '.join(all_categories)}."
            matched_category = match[0]
            matching_rows = [r for r in rows if r.get("categoria", "") == matched_category]
            lines = [f"Productos en la categoría '{matched_category}':\n"]
            for row in matching_rows:
                lines.append(f"  • {row['producto']} — {_format_price(row['precio'])} por {row['unidad']}")
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

    def _build_generate_payment_link(
        self, sheets: SheetsClient, sheet_id: str, access_token: str, sandbox: bool,
        redis: Any, client_id: str, user_phone: str
    ) -> dict:
        mp = MercadoPagoClient(access_token=access_token, sandbox=sandbox)

        async def generate_payment_link(items: list[dict], sucursal: str) -> str:
            mp_items: list[dict] = []
            resolved_lines: list[str] = []

            for item in items:
                product  = item.get("product", "")
                quantity = int(item.get("quantity", 1))
                rows = sheets.find_products(sheet_id, product, score_cutoff=85)
                if not rows:
                    rows = sheets.find_products(sheet_id, product)
                if not rows:
                    return f"No encontré el producto '{product}' en el catálogo."
                if len(rows) > 1:
                    options = "\n".join(f"- {r['producto']}" for r in rows)
                    return f"Encontré varias variantes de '{product}'. Especificá cuál querés:\n{options}"
                row = rows[0]
                try:
                    unit_price = _parse_price(str(row["precio"]))
                except ValueError as e:
                    logger.error("Price parse error for '%s': %s", row["producto"], e)
                    return "Hubo un problema con el precio de un producto. Llamá al local."
                mp_items.append({"title": row["producto"], "quantity": quantity, "unit_price": unit_price})
                resolved_lines.append(f"  • {quantity}x {row['producto']} — ${unit_price * quantity:,.0f} ARS")

            try:
                preference = await mp.create_payment_link(items=mp_items)
            except MercadoPagoError as e:
                logger.error("MercadoPago error: %s", e)
                return "No pude generar el link de pago en este momento. Intentá más tarde o llamá al local."

            grand_total = sum(i["unit_price"] * i["quantity"] for i in mp_items)
            redis.set(f"mp_payment:{preference.preference_id}", json.dumps({
                "user_phone": user_phone, "client_id": client_id,
                "sucursal": sucursal, "items": mp_items, "total": grand_total,
            }), ex=MP_PAYMENT_TTL)

            return (
                f"Acá tenés el link para pagar tu pedido en {sucursal}:\n"
                f"{chr(10).join(resolved_lines)}\n"
                f"*Total: ${grand_total:,.0f} ARS*\n\n"
                f"{preference.init_point}\n\n"
                f"Cuando lo pagues, pasás a retirarlo en el local."
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
            "handler": generate_payment_link,
        }
