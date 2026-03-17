import json
import logging
from typing import Optional, Any

from rapidfuzz import process, fuzz
from upstash_redis import Redis

from app.clients.models import ClientConfig
from app.integrations.sheets import SheetsClient
from app.integrations.mercadopago import MercadoPagoClient, MercadoPagoError

logger = logging.getLogger(__name__)

MP_PAYMENT_TTL = 86400

def build_tools(config: ClientConfig, sheets: SheetsClient, redis: Any = None, user_phone: str = "", client_id: str = "") -> list:
    """
    Returns the list of tool definitions enabled for this client.
    Only tools listed in config.tools_enabled are included.
    Tools use native Anthropic function calling format.
    """
    all_tools = {
        "get_price": _make_get_price(config, sheets),
        "get_stock": _make_get_stock(config, sheets),
        "get_all_products": _make_get_all_products(config, sheets),
        "get_products_by_category": _make_get_products_by_category(config, sheets),
        "get_hours": _make_get_hours(config),
    }
    
    if config.mp_access_token and redis and user_phone and config.sheet_id:
        all_tools["generate_payment_link"] = _build_generate_payment_link(
            sheets=sheets,
            sheet_id=config.sheet_id,
            access_token=config.mp_access_token,
            sandbox=config.mp_sandbox,
            redis=redis,
            client_id=str(config.id),
            user_phone=user_phone,
        )

    # ── Calendar tools ────────────────────────────────────────────────────────
    _calendar_tool_names = {
        "check_availability", "book_appointment", "get_appointment",
        "cancel_appointment", "reschedule_appointment", "get_current_date_hour",
    }
    if _calendar_tool_names.intersection(config.tools_enabled) and redis is not None:
        from app.agent.calendar_tools import build_calendar_tools

        if config.calendar_id:
            # Real Google Calendar integration
            from app.integrations.calendar import GoogleCalendarClient
            _calendar = GoogleCalendarClient(
                calendar_id=config.calendar_id,
                redis=redis,
                client_id=client_id or str(config.id),
            )
        else:
            # Demo / local fallback
            from app.integrations.calendar_mock import CalendarMock
            _calendar = CalendarMock(redis=redis, client_id=client_id or str(config.id))

        for ct in build_calendar_tools(_calendar):
            all_tools[ct["definition"]["name"]] = ct

    return [all_tools[name] for name in config.tools_enabled if name in all_tools]


# --- Tool factories ---
# Each returns a dict with `definition` (sent to AI API) and `handler` (called on tool_use)

def _format_price(precio: Any) -> str:
    """Format price, handling non-numeric values like 'por encargue' or 'sin stock'."""
    try:
        return f"${float(precio):,.0f}"
    except (ValueError, TypeError):
        return str(precio)


def _make_get_price(config: ClientConfig, sheets: SheetsClient) -> dict:
    def handler(product: str) -> str:
        if not config.sheet_id:
            return "No hay información de precios disponible."

        rows = sheets.find_products(config.sheet_id, product)
        if not rows:
            return f"No encontré '{product}' en el catálogo."

        if len(rows) == 1:
            row = rows[0]
            precio_fmt = _format_price(row['precio'])
            return f"{row['producto']}: {precio_fmt} por {row['unidad']}."

        lines = [f"{r['producto']}: {_format_price(r['precio'])} por {r['unidad']}" for r in rows]
        return "\n".join(lines)

    return {
        "definition": {
            "name": "get_price",
            "description": "Consulta el precio de un producto. Si hay varias variantes, devuelve todos los precios.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Nombre o categoría del producto (ej: 'tornillo', 'pintura')",
                    }
                },
                "required": ["product"],
            },
        },
        "handler": handler,
    }


def _make_get_stock(config: ClientConfig, sheets: SheetsClient) -> dict:
    def handler(product: str) -> str:
        if not config.sheet_id:
            return "No hay información de stock disponible."

        rows = sheets.find_products(config.sheet_id, product)
        if not rows:
            return f"No encontré '{product}' en el catálogo."

        if len(rows) == 1:
            row = rows[0]
            stock = int(row["stock"])
            if stock == 0:
                return f"{row['producto']}: sin stock por el momento."
            return f"Sí, tenemos {row['producto']}."

        # Multiple variants — list them without quantities
        in_stock = [r for r in rows if int(r["stock"]) > 0]
        out_of_stock = [r for r in rows if int(r["stock"]) == 0]

        if not in_stock:
            return f"No tenemos {product} en stock en este momento."

        variant_names = ", ".join(r["producto"] for r in in_stock)
        result = f"Sí, tenemos: {variant_names}."
        if out_of_stock:
            result += f" Sin stock: {', '.join(r['producto'] for r in out_of_stock)}."
        return result

    return {
        "definition": {
            "name": "get_stock",
            "description": "Consulta si hay stock de un producto. Si hay varias variantes, devuelve todas.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Nombre o categoría del producto (ej: 'tornillo', 'pintura', 'cable')",
                    }
                },
                "required": ["product"],
            },
        },
        "handler": handler,
    }


def _make_get_all_products(config: ClientConfig, sheets: SheetsClient) -> dict:
    def handler() -> str:
        if not config.sheet_id:
            return "No hay catálogo disponible."
        rows = sheets.get_all_rows(config.sheet_id)
        if not rows:
            return "No se pudo cargar el catálogo en este momento."

        # Group by category
        by_category: dict[str, list[str]] = {}
        for row in rows:
            cat = row.get("categoria", "General")
            precio_fmt = _format_price(row['precio'])
            product_line = f"  • {row['producto']} — {precio_fmt} por {row['unidad']}"
            by_category.setdefault(cat, []).append(product_line)

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
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        "handler": handler,
    }


def _make_get_hours(config: ClientConfig) -> dict:
    def handler() -> str:
        # Hours are embedded in the system prompt — extract the line that starts with "Horario:"
        for line in config.system_prompt.splitlines():
            if line.strip().lower().startswith("horario"):
                return line.strip()
        return "Consultá directamente con el local para conocer el horario."

    return {
        "definition": {
            "name": "get_hours",
            "description": "Devuelve el horario de atención del local.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        "handler": handler,
    }


def _make_get_products_by_category(config: ClientConfig, sheets: SheetsClient) -> dict:
    def handler(category: str) -> str:
        if not config.sheet_id:
            return "No hay catálogo disponible."
        rows = sheets.get_all_rows(config.sheet_id)
        if not rows:
            return "No se pudo cargar el catálogo en este momento."

        # Get unique categories from sheet
        all_categories = [str(row.get("categoria", "")) for row in rows]
        unique_categories = list(dict.fromkeys(c for c in all_categories if c))

        # Fuzzy match the requested category against real ones
        match = process.extractOne(
            category,
            unique_categories,
            scorer=fuzz.partial_ratio,
            score_cutoff=60,
        )
        if not match:
            return f"No encontré la categoría '{category}'. Las categorías disponibles son: {', '.join(unique_categories)}."

        matched_category = match[0]
        matching_rows = [r for r in rows if r.get("categoria", "") == matched_category]

        lines = [f"Productos en la categoría '{matched_category}':\n"]
        for row in matching_rows:
            precio_fmt = _format_price(row["precio"])
            lines.append(f"  • {row['producto']} — {precio_fmt} por {row['unidad']}")

        return "\n".join(lines).strip()

    return {
        "definition": {
            "name": "get_products_by_category",
            "description": "Busca productos por categoría (ej: 'pinturas', 'herramientas'). Devuelve la lista de productos en esa categoría.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Nombre de la categoría a consultar",
                    }
                },
                "required": ["category"],
            },
        },
        "handler": handler,
    }


def _parse_price(raw: str) -> float:
    """Parses ARS price strings like '$1.250,50' or '1250.50' → float."""
    cleaned = str(raw).strip().replace("$", "").replace(" ", "")
    # ARS format: dots are thousands separators, comma is decimal
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    return float(cleaned)


# ── generate_payment_link ─────────────────────────────────────────────────────

def _build_generate_payment_link(
    sheets: SheetsClient,
    sheet_id: str,
    access_token: str,
    sandbox: bool,
    redis: Redis,
    client_id: str,
    user_phone: str,
) -> dict:
    mp = MercadoPagoClient(access_token=access_token, sandbox=sandbox)

    async def generate_payment_link(
        items: list[dict],
        sucursal: str,
    ) -> str:
        """
        items: [{"product": "medialuna de manteca", "quantity": 12, "unit_price": 350}, ...]
        sucursal: branch/location name for the confirmation message.
        """
        mp_items: list[dict] = []
        resolved_lines: list[str] = []

        for item in items:
            product = item.get("product", "")
            quantity = int(item.get("quantity", 1))
            # LLM can optionally provide unit_price; we always verify against the catalog.
            rows = sheets.find_products(sheet_id, product, score_cutoff=85)
            if not rows:
                rows = sheets.find_products(sheet_id, product)
            if not rows:
                return f"No encontré el producto '{product}' en el catálogo."

            if len(rows) > 1:
                options = "\n".join(f"- {r['producto']}" for r in rows)
                return (
                    f"Encontré varias variantes de '{product}'. "
                    f"Especificá cuál querés:\n{options}"
                )

            row = rows[0]
            try:
                unit_price = _parse_price(str(row["precio"]))
            except ValueError as e:
                logger.error("Price parse error for '%s': %s", row["producto"], e)
                return "Hubo un problema con el precio de un producto. Llamá al local para coordinar el pago."

            mp_items.append({
                "title": row["producto"],
                "quantity": quantity,
                "unit_price": unit_price,
            })
            line_total = unit_price * quantity
            resolved_lines.append(
                f"  • {quantity}x {row['producto']} — ${line_total:,.0f} ARS"
            )

        # Create a single MP preference with all items
        try:
            preference = await mp.create_payment_link(items=mp_items)
        except MercadoPagoError as e:
            logger.error("MercadoPago error generating multi-item preference: %s", e)
            return "No pude generar el link de pago en este momento. Intentá más tarde o llamá al local."

        grand_total = sum(i["unit_price"] * i["quantity"] for i in mp_items)

        # Store metadata in Redis
        payment_meta = {
            "user_phone": user_phone,
            "client_id": client_id,
            "sucursal": sucursal,
            "items": mp_items,
            "total": grand_total,
        }
        redis_key = f"mp_payment:{preference.preference_id}"
        redis.set(redis_key, json.dumps(payment_meta), ex=MP_PAYMENT_TTL)
        logger.info(
            "Stored MP payment meta [key=%s phone=%s items=%d total=%.2f sucursal=%s]",
            redis_key, user_phone, len(mp_items), grand_total, sucursal,
        )

        item_summary = "\n".join(resolved_lines)
        return (
            f"Acá tenés el link para pagar tu pedido en {sucursal}:\n"
            f"{item_summary}\n"
            f"*Total: ${grand_total:,.0f} ARS*\n\n"
            f"{preference.init_point}\n\n"
            f"Cuando lo pagues, pasás a retirarlo en el local."
        )

    return {
        "definition": {
            "name": "generate_payment_link",
            "description": (
                "Genera un link de pago de MercadoPago para uno o más productos. "
                "Usá esta herramienta SOLO si el cliente dijo explícitamente que quiere pagar o comprar, "
                "Y ya confirmó todos los productos exactos y las cantidades. "
                "Pasá todos los items del pedido en una sola llamada."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Lista de productos a incluir en el pedido.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "product": {
                                    "type": "string",
                                    "description": "Nombre exacto del producto",
                                },
                                "quantity": {
                                    "type": "integer",
                                    "description": "Cantidad de unidades",
                                    "minimum": 1,
                                },
                            },
                            "required": ["product", "quantity"],
                        },
                        "minItems": 1,
                    },
                    "sucursal": {
                        "type": "string",
                        "description": "Nombre de la sucursal o local donde se retira el pedido",
                    },
                },
                "required": ["items", "sucursal"],
            },
        },
        "handler": generate_payment_link,
    }