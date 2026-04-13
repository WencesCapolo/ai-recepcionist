"""
app/agent/tools.py

Public API for tool construction and dispatch.

    build_tools_for_client(config, *, sheets, redis, user_phone) -> list[dict]
        Builds [{definition, handler}] for every tool in config.tools_enabled
        that has its required tool_config present.

    run_tool(tool_name, tool_input, handler_map) -> str
        Dispatches a single LLM tool call. handler_map comes from
        build_tools_for_client. Returns a Spanish error string on unknown tool.

graph.py is the only caller of these functions.
"""

import logging
from typing import Any

from app.clients.models import ClientConfig
from app.integrations.sheets import SheetsClient
from app.agent.shared_tools import make_get_current_date_hour, make_get_hours
from app.agent.verticals.retail import build_retail_tools
from app.agent.verticals.calendar_booking import build_calendar_tools
from app.agent.verticals.dentist import build_dentist_tools
from app.agent.verticals.padel import build_padel_tools

logger = logging.getLogger(__name__)


def build_tools_for_client(
    config: ClientConfig,
    sheets: SheetsClient,
    redis: Any = None,
    user_phone: str = "",
    client_id: str = "",
) -> list[dict]:
    """
    Returns [{definition, handler}] for every tool in config.tools_enabled
    whose required tool_config entry is present.

    Shared tools (get_current_date_hour, get_hours) are always included if
    they appear in tools_enabled — they have no tool_config gate.

    For tools with the same name across verticals (e.g. get_price in retail
    and padel), the last builder to register wins. Clients should only have
    one vertical active at a time.
    """
    _cid = client_id or str(config.id)
    all_tools: dict[str, dict] = {}

    # Shared tools — no tool_config requirement
    all_tools["get_current_date_hour"] = make_get_current_date_hour()
    all_tools["get_hours"] = make_get_hours(config)

    # Vertical tools — each builder checks its own tool_config gate internally
    for tool in build_retail_tools(config, sheets=sheets, redis=redis, user_phone=user_phone):
        all_tools[tool["definition"]["name"]] = tool

    for tool in build_calendar_tools(config, redis=redis, client_id=_cid):
        all_tools[tool["definition"]["name"]] = tool

    for tool in build_dentist_tools(config, sheets=sheets):
        all_tools[tool["definition"]["name"]] = tool

    for tool in build_padel_tools(config, redis=redis, user_phone=user_phone, client_id=_cid):
        all_tools[tool["definition"]["name"]] = tool

    return [all_tools[name] for name in config.tools_enabled if name in all_tools]


async def run_tool(tool_name: str, tool_input: dict, handler_map: dict) -> str:
    """
    Dispatch a tool call from the LLM to the correct handler.
    Injects no config — all context is already captured by the handler closure.
    Returns a Spanish error string if the tool is not in handler_map.
    """
    handler = handler_map.get(tool_name)
    if handler is None:
        logger.warning("Unknown tool requested: %s", tool_name)
        return f"Herramienta '{tool_name}' no disponible."
    return await handler(**tool_input)
