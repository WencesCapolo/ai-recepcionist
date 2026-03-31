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

    _calendar_tool_names = {
        "check_availability", "book_appointment", "get_appointment",
        "cancel_appointment", "reschedule_appointment", "get_current_date_hour",
        "get_treatment_info",
    }
    if _calendar_tool_names.intersection(config.tools_enabled) and redis is not None:
        from app.agent.calendar_tools import build_calendar_tools

        if config.calendar_id:
            from app.integrations.calendar import GoogleCalendarClient
            _calendar = GoogleCalendarClient(
                calendar_id=config.calendar_id,
                redis=redis,
                client_id=client_id or str(config.id),
            )
        else:
            from app.integrations.calendar_mock import CalendarMock
            _calendar = CalendarMock(redis=redis, client_id=client_id or str(config.id))

        for ct in build_calendar_tools(_calendar):
            all_tools[ct["definition"]["name"]] = ct

    # ── Dentist info tools ────────────────────────────────────────────────────
    _dentist_info_names = {"get_prices", "get_insurances", "get_treatment_info"}
    if _dentist_info_names.intersection(config.tools_enabled) and config.prices_sheet_id:
        all_tools["get_treatment_info"] = _make_get_treatment_info(sheets, config.prices_sheet_id)
        all_tools["get_prices"]         = _make_get_prices_dentist(sheets, config.prices_sheet_id)
        all_tools["get_insurances"]     = _make_get_insurances(sheets, config.prices_sheet_id)
    # ── Padel tools ───────────────────────────────────────────────────────────
    _PADEL_TRIGGER = {"get_availability", "create_booking", "cancel_booking"}
    if _PADEL_TRIGGER & set(config.tools_enabled) and redis is not None and config.calendar_id:
        from app.agent.padel_tools import build_padel_tools, build_padel_payment_tool
        from app.integrations.padel_calendar import PadelCalendarClient

        _padel = PadelCalendarClient(
            calendar_id=config.calendar_id,
            redis=redis,
            client_id=client_id or str(config.id),
        )
        for pt in build_padel_tools(_padel, config):
            all_tools[pt["definition"]["name"]] = pt

        if (
            config.mp_access_token
            and user_phone
            and "generate_padel_payment_link" in config.tools_enabled
        ):
            all_tools["generate_padel_payment_link"] = build_padel_payment_tool(
                padel=_padel,
                config=config,
                redis=redis,
                user_phone=user_phone,
                client_id=client_id or str(config.id),
            )

    return [all_tools[name] for name in config.tools_enabled if name in all_tools]


# --- Tool factories ---

def _format_price(precio: Any) -> str:
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
            return f"{row['producto']}: {_format_price(row['precio'])} por {row['unidad']}."
        lines = [f"{r['producto']}: {_format_price(r['precio'])} por {r['unidad']}" for r in rows]
        return "\n".join(lines)

    return {
        "definition": {
            "name": "get_price",
            "description": "Consulta el precio de un producto. Si hay varias variantes, devuelve todos los precios.",
            "input_schema": {
                "type": "object",
                "properties": {"product": {"type": "string", "description": "Nombre o categoría del producto (ej: 'tornillo', 'pintura')"}},
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
                "properties": {"product": {"type": "string", "description": "Nombre o categoría del producto (ej: 'tornillo', 'pintura', 'cable')"}},
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


def _make_get_hours(config: ClientConfig) -> dict:
    def handler() -> str:
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


def _make_get_products_by_category(config: ClientConfig, sheets: SheetsClient) -> dict:
    def handler(category: str) -> str:
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
            "description": "Busca productos por categoría (ej: 'pinturas', 'herramientas'). Devuelve la lista de productos en esa categoría.",
            "input_schema": {
                "type": "object",
                "properties": {"category": {"type": "string", "description": "Nombre de la categoría a consultar"}},
                "required": ["category"],
            },
        },
        "handler": handler,
    }


def _parse_price(raw: str) -> float:
    cleaned = str(raw).strip().replace("$", "").replace(" ", "")
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
                return "Hubo un problema con el precio de un producto. Llamá al local para coordinar el pago."
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
        logger.info("Stored MP payment meta [key=mp_payment:%s phone=%s total=%.2f]",
                    preference.preference_id, user_phone, grand_total)

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
                                "product":  {"type": "string",  "description": "Nombre exacto del producto"},
                                "quantity": {"type": "integer", "description": "Cantidad de unidades", "minimum": 1},
                            },
                            "required": ["product", "quantity"],
                        },
                        "minItems": 1,
                    },
                    "sucursal": {"type": "string", "description": "Nombre de la sucursal o local donde se retira el pedido"},
                },
                "required": ["items", "sucursal"],
            },
        },
        "handler": generate_payment_link,
    }


# ── Dentist tools ─────────────────────────────────────────────────────────────

def _make_get_treatment_info(sheets: SheetsClient, sheet_id: str) -> dict:
    def handler(treatment: str) -> str:
        from app.integrations.dentist_sheets import get_treatment_info
        return get_treatment_info(sheets, sheet_id, treatment)

    return {
        "definition": {
            "name": "get_treatment_info",
            "description": (
                "Devuelve la duración en minutos y el precio de un tratamiento odontológico. "
                "Llamar ANTES de check_availability para pasar el duration_minutes correcto."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "treatment": {
                        "type": "string",
                        "description": "Nombre del tratamiento (ej: 'limpieza', 'extracción', 'conducto')",
                    }
                },
                "required": ["treatment"],
            },
        },
        "handler": handler,
    }


def _make_get_prices_dentist(sheets: SheetsClient, sheet_id: str) -> dict:
    def handler(treatment: str = "") -> str:
        from app.integrations.dentist_sheets import get_all_treatments, get_treatment_info
        if not treatment:
            return get_all_treatments(sheets, sheet_id)
        raw  = get_treatment_info(sheets, sheet_id, treatment)
        data = json.loads(raw)
        name  = data.get("name", treatment)
        price = data.get("price")
        dur   = data.get("duration_minutes", 30)
        note  = data.get("note", "")
        if note and not price:
            return note
        if price:
            return f"{name}: ${price:,.0f} ({dur} min)"
        return f"{name}: precio a consultar ({dur} min)"

    return {
        "definition": {
            "name": "get_prices",
            "description": (
                "Devuelve el precio y duración de un tratamiento odontológico. "
                "Si no se especifica tratamiento, devuelve el listado completo de aranceles."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "treatment": {
                        "type": "string",
                        "description": "Nombre del tratamiento (ej: 'limpieza', 'extracción', 'conducto'). Vacío para ver todo.",
                    }
                },
                "required": [],
            },
        },
        "handler": handler,
    }


def _make_get_insurances(sheets: SheetsClient, sheet_id: str) -> dict:
    def handler() -> str:
        from app.integrations.dentist_sheets import get_insurances
        return get_insurances(sheets, sheet_id)

    return {
        "definition": {
            "name": "get_insurances",
            "description": (
                "Devuelve la lista de obras sociales y prepagas aceptadas por el consultorio. "
                "Llamar cuando el paciente pregunta si aceptan su obra social o prepaga."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        "handler": handler,
    }