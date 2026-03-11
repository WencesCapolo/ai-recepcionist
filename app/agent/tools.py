import logging
from typing import Optional

from app.clients.models import ClientConfig
from app.integrations.sheets import SheetsClient
from rapidfuzz import process, fuzz

logger = logging.getLogger(__name__)


def build_tools(config: ClientConfig, sheets: SheetsClient) -> list:
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