import json
from typing import Any

import logfire

from app.clients.models import ClientConfig, DentistSheetsConfig
from app.integrations.sheets import SheetsClient


def build_dentist_tools(
    config: ClientConfig,
    *,
    sheets: SheetsClient,
    **_: Any,
) -> list[dict]:
    cfg = config.tool_config.dentist_sheets
    if cfg is None:
        return []

    tools = []
    enabled = frozenset(config.tools_enabled)

    if "get_treatment_info" in enabled:
        tools.append(_make_get_treatment_info(cfg, sheets, config))
    if "get_prices" in enabled:
        tools.append(_make_get_prices(cfg, sheets, config))
    if "get_insurances" in enabled:
        tools.append(_make_get_insurances(cfg, sheets, config))

    return tools


def _make_get_treatment_info(cfg: DentistSheetsConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler(treatment: str) -> str:
        with logfire.span("tool.get_treatment_info", client_id=str(config.id)):
            from app.integrations.dentist_sheets import get_treatment_info
            return get_treatment_info(sheets, cfg.sheet_id, treatment, tab_treatments=cfg.tab_treatments)

    return {
        "definition": {
            "name": "get_treatment_info",
            "description": "Devuelve tiempo y precio de tratamiento odontológico. Llamar ANTES de check_availability.",
            "input_schema": {
                "type": "object",
                "properties": {"treatment": {"type": "string"}},
                "required": ["treatment"],
            },
        },
        "handler": handler,
    }


def _make_get_prices(cfg: DentistSheetsConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler(treatment: str = "") -> str:
        with logfire.span("tool.get_prices", client_id=str(config.id)):
            from app.integrations.dentist_sheets import get_all_treatments, get_treatment_info
            if not treatment:
                return get_all_treatments(sheets, cfg.sheet_id, tab_treatments=cfg.tab_treatments)
            raw  = get_treatment_info(sheets, cfg.sheet_id, treatment, tab_treatments=cfg.tab_treatments)
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
            "description": "Precio y duración de tratamiento. Vacío para ver todo.",
            "input_schema": {
                "type": "object",
                "properties": {"treatment": {"type": "string"}},
                "required": [],
            },
        },
        "handler": handler,
    }


def _make_get_insurances(cfg: DentistSheetsConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler() -> str:
        with logfire.span("tool.get_insurances", client_id=str(config.id)):
            from app.integrations.dentist_sheets import get_insurances
            return get_insurances(sheets, cfg.sheet_id, tab_insurances=cfg.tab_insurances)

    return {
        "definition": {
            "name": "get_insurances",
            "description": "Obras sociales aceptadas.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        "handler": handler,
    }
