import json
from typing import Any
from app.clients.models import ClientConfig
from app.integrations.sheets import SheetsClient
from app.agent.registry import DENTIST_INFO_TOOLS

class DentistToolset:
    name = "dentist"
    required_tools = frozenset(t.value for t in DENTIST_INFO_TOOLS)

    def is_applicable(self, config: ClientConfig) -> bool:
        enabled = frozenset(config.tools_enabled)
        return bool(config.prices_sheet_id and (enabled & self.required_tools))

    def build(self, config: ClientConfig, **deps: Any) -> list[dict]:
        sheets: SheetsClient = deps["sheets"]
        
        all_tools = []
        if "get_treatment_info" in config.tools_enabled:
            all_tools.append(self._make_get_treatment_info(sheets, config))
        if "get_prices" in config.tools_enabled:
            all_tools.append(self._make_get_prices_dentist(sheets, config))
        if "get_insurances" in config.tools_enabled:
            all_tools.append(self._make_get_insurances(sheets, config))
            
        return all_tools

    def _make_get_treatment_info(self, sheets: SheetsClient, config: ClientConfig) -> dict:
        async def handler(treatment: str) -> str:
            from app.integrations.dentist_sheets import get_treatment_info
            # Synchronous call wrapped in async function
            return get_treatment_info(
                sheets, config.prices_sheet_id, treatment, tab_treatments=config.sheet_tab_treatments
            )
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

    def _make_get_prices_dentist(self, sheets: SheetsClient, config: ClientConfig) -> dict:
        async def handler(treatment: str = "") -> str:
            from app.integrations.dentist_sheets import get_all_treatments, get_treatment_info
            if not treatment:
                return get_all_treatments(sheets, config.prices_sheet_id, tab_treatments=config.sheet_tab_treatments)
            raw  = get_treatment_info(sheets, config.prices_sheet_id, treatment, tab_treatments=config.sheet_tab_treatments)
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

    def _make_get_insurances(self, sheets: SheetsClient, config: ClientConfig) -> dict:
        async def handler() -> str:
            from app.integrations.dentist_sheets import get_insurances
            return get_insurances(sheets, config.prices_sheet_id, tab_insurances=config.sheet_tab_insurances)

        return {
            "definition": {
                "name": "get_insurances",
                "description": "Obras sociales aceptadas.",
                "input_schema": {"type": "object", "properties": {}, "required": []},
            },
            "handler": handler,
        }
