"""
app/integrations/dentist_sheets.py

Dentist-specific Google Sheets helpers.
Reads from a spreadsheet with three tabs:
  - "Tratamientos"  → Tratamiento | Duracion | Precio | Descripcion
  - "Obras Sociales" → Obra Social | Cobertura | Observaciones

These methods are meant to be mixed into or called from the existing SheetsClient.
Alternatively, instantiate DentistSheetsClient directly if you prefer separation.

Sheet ID is taken from ClientConfig.prices_sheet_id (reusing the same field).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


def _get_gc():
    """Returns the cached gspread client from SheetsClient's module."""
    from app.integrations.sheets import _get_gspread_client
    return _get_gspread_client()




# Tab names — change here if the dentist names them differently
TAB_TREATMENTS = "Tratamientos"
TAB_INSURANCES  = "Obras Sociales"


# ---------------------------------------------------------------------------
# Standalone helpers (call these from your existing SheetsClient methods,
# or add them as methods directly on SheetsClient)
# ---------------------------------------------------------------------------

def get_treatment_info(sheets_client, sheet_id: str, treatment: str) -> str:
    """
    Returns JSON string: {"duration_minutes": 30, "price": 5000, "name": "Limpieza dental"}
    Used by GoogleCalendarClient.get_treatment_info() and the get_prices tool.
    """
    try:
        spreadsheet = _get_gc().open_by_key(sheet_id)
        ws          = spreadsheet.worksheet(TAB_TREATMENTS)
        records     = ws.get_all_records()
    except Exception as e:
        logger.error("get_treatment_info sheet error: %s", e)
        return json.dumps({"duration_minutes": 30, "price": None, "note": str(e)})

    if not records:
        return json.dumps({"duration_minutes": 30, "price": None, "note": "Hoja vacía"})

    names = [str(r.get("Tratamiento", "")) for r in records]
    match = process.extractOne(
        treatment,
        names,
        scorer=fuzz.partial_ratio,
        score_cutoff=55,
    )

    if not match:
        available = ", ".join(names)
        return json.dumps({
            "duration_minutes": 30,
            "price": None,
            "note": f"No encontré '{treatment}'. Tratamientos disponibles: {available}",
        })

    row      = records[names.index(match[0])]
    duration = _parse_int(row.get("Duracion", 30))
    price    = _parse_price_nullable(row.get("Precio"))

    return json.dumps({
        "duration_minutes": duration,
        "price":            price,
        "name":             row.get("Tratamiento", match[0]),
        "description":      row.get("Descripcion", ""),
    })


def get_all_treatments(sheets_client, sheet_id: str) -> str:
    """
    Returns a human-readable list of all treatments with duration and price.
    Used by the get_prices tool.
    """
    try:
        spreadsheet = _get_gc().open_by_key(sheet_id)
        ws          = spreadsheet.worksheet(TAB_TREATMENTS)
        records     = ws.get_all_records()
    except Exception as e:
        logger.error("get_all_treatments error: %s", e)
        return "No pude obtener los aranceles en este momento."

    if not records:
        return "No hay tratamientos cargados en la hoja."

    lines = []
    for r in records:
        name     = r.get("Tratamiento", "")
        duration = _parse_int(r.get("Duracion", 30))
        price    = r.get("Precio", "")
        desc     = r.get("Descripcion", "")
        line     = f"• {name}: ${price} ({duration} min)"
        if desc:
            line += f" — {desc}"
        lines.append(line)

    return "Tratamientos y aranceles:\n" + "\n".join(lines)


def get_insurances(sheets_client, sheet_id: str) -> str:
    """
    Returns a human-readable list of accepted obras sociales.
    """
    try:
        spreadsheet = _get_gc().open_by_key(sheet_id)
        ws          = spreadsheet.worksheet(TAB_INSURANCES)
        records     = ws.get_all_records()
    except Exception as e:
        logger.error("get_insurances error: %s", e)
        return "No pude obtener la información de obras sociales en este momento."

    if not records:
        return "No hay obras sociales cargadas."

    lines = []
    for r in records:
        name        = r.get("Obra Social", "")
        coverage    = r.get("Cobertura", "")
        observation = r.get("Observaciones", "")
        line        = f"• {name}"
        if coverage:
            line += f": {coverage}"
        if observation:
            line += f" ({observation})"
        lines.append(line)

    return "Obras sociales aceptadas:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_int(val: Any, default: int = 30) -> int:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _parse_price_nullable(val: Any) -> Optional[float]:
    if not val:
        return None
    try:
        cleaned = str(val).strip().replace("$", "").replace(".", "").replace(",", ".")
        return float(cleaned)
    except (ValueError, TypeError):
        return None