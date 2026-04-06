import logging
from typing import Any

from app.clients.models import ClientConfig
from app.integrations.sheets import SheetsClient
from app.agent.base_toolset import ToolsetProvider
from app.agent.verticals.retail import RetailToolset
from app.agent.verticals.calendar_booking import CalendarBookingToolset
from app.agent.verticals.dentist import DentistToolset
from app.agent.verticals.padel import PadelToolset

logger = logging.getLogger(__name__)

_TOOLSETS: list[ToolsetProvider] = [
    RetailToolset(),
    CalendarBookingToolset(),
    DentistToolset(),
    PadelToolset(),
]

def build_tools(config: ClientConfig, sheets: SheetsClient, redis: Any = None, user_phone: str = "", client_id: str = "") -> list[dict]:
    deps = {"sheets": sheets, "redis": redis, "user_phone": user_phone, "client_id": client_id}
    all_tools: dict[str, dict] = {}
    
    for toolset in _TOOLSETS:
        if toolset.is_applicable(config):
            for tool in toolset.build(config, **deps):
                all_tools[tool["definition"]["name"]] = tool
                
    return [all_tools[name] for name in config.tools_enabled if name in all_tools]