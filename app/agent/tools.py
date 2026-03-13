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
    if (
        (
            "check_availability" in config.tools_enabled
            or "book_appointment" in config.tools_enabled
        )
        and redis is not None
    ):
        from app.agent.calendar_tools import build_calendar_tools
        from app.integrations.calendar_mock import CalendarMock

        _calendar = CalendarMock(redis=redis, client_id=client_id or str(config.id))
        calendar_tool_list = build_calendar_tools(_calendar)
        # Index by name so the tools_enabled filter below works uniformly
        for ct in calendar_tool_list:
            all_tools[ct["definition"]["name"]] = ct

    return [all_tools[name] for name in config.tools_enabled if name in all_tools]


# --- Tool factories ---
# Each returns a dict with `definition` (sent to AI API) and `handler` (called on tool_use)

def _make_get_price(config: ClientConfig, sheets: SheetsClient) -> dict:
    def handler(product: str) -> str:
        if not config.sheet_id:
            return "No hay información de precios disponible."

        rows = sheets.find_products(config.sheet_id, product)
        if not rows:
            return f"No encontré '{product}' en el catálogo."

        if len(rows) == 1:
            row = rows[0]
            return f"{row['producto']}: ${int(row['precio']):,} por {row['unidad']}."

        lines = [f"{r['producto']}: ${int(r['precio']):,} por {r['unidad']}" for r in rows]
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
            product_line = f"  • {row['producto']} — ${row['precio']:,} por {row['unidad']}"
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
            precio = int(row["precio"])
            lines.append(f"  • {row['producto']} — ${precio:,} por {row['unidad']}")

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
 
    async def generate_payment_link(product: str, quantity: int) -> str:
        # 1. Find product — higher cutoff to avoid variant bleed
        rows = sheets.find_products(sheet_id, product, score_cutoff=85)
 
        if not rows:
            # Fallback to standard cutoff before giving up
            rows = sheets.find_products(sheet_id, product)
            if not rows:
                return f"No encontré el producto '{product}' en el catálogo."
 
        # 2. Multiple variants — ask customer to be specific
        if len(rows) > 1:
            options = "\n".join(f"- {r['producto']}" for r in rows)
            return (
                f"Encontré varias variantes de '{product}'. "
                f"Especificá cuál querés:\n{options}"
            )
 
        row = rows[0]
 
        # 3. Parse price
        try:
            unit_price = _parse_price(str(row["precio"]))
        except ValueError as e:
            logger.error("Price parse error for '%s': %s", row["producto"], e)
            return "Hubo un problema con el precio del producto. Llamá al local para coordinar el pago."
 
        # 4. Create MP preference
        try:
            preference = await mp.create_payment_link(
                title=row["producto"],
                unit_price=unit_price,
                quantity=quantity,
            )
        except MercadoPagoError as e:
            logger.error("MercadoPago error for '%s': %s", row["producto"], e)
            return "No pude generar el link de pago en este momento. Intentá más tarde o llamá al local."
 
        # 5. Store metadata in Redis so mp_handler can look up who paid
        payment_meta = {
            "user_phone": user_phone,
            "client_id": client_id,
            "product": row["producto"],
            "quantity": quantity,
            "unit_price": unit_price,
            "total": unit_price * quantity,
        }
        redis_key = f"mp_payment:{preference.preference_id}"
        redis.set(redis_key, json.dumps(payment_meta), ex=MP_PAYMENT_TTL)
        logger.info(
            "Stored MP payment meta [key=%s phone=%s product=%s]",
            redis_key, user_phone, row["producto"],
        )
 
        total = unit_price * quantity
        return (
            f"Acá tenés el link para pagar {quantity}x {row['producto']} "
            f"(total: ${total:,.0f} ARS):\n{preference.init_point}\n"
            f"Cuando lo pagues, pasás a retirarlo en el local."
        )
 
    return {
        "definition": {
            "name": "generate_payment_link",
            "description": (
                "Genera un link de pago de MercadoPago para un producto. "
                "Usá esta herramienta SOLO si el cliente dijo explícitamente que quiere pagar o comprar, "
                "Y ya confirmó el producto exacto Y la cantidad."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Nombre exacto del producto a comprar",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Cantidad de unidades",
                        "minimum": 1,
                    },
                },
                "required": ["product", "quantity"],
            },
        },
        "handler": generate_payment_link,
    }
 