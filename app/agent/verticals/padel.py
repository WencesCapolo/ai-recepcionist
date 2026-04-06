from typing import Any

from app.clients.models import ClientConfig
from app.agent.registry import PADEL_TOOLS


class PadelToolset:
    name = "padel"
    required_tools = frozenset(t.value for t in PADEL_TOOLS)

    def is_applicable(self, config: ClientConfig) -> bool:
        enabled = frozenset(config.tools_enabled)
        return bool(enabled & self.required_tools)

    def build(self, config: ClientConfig, **deps: Any) -> list[dict]:
        redis = deps.get("redis")
        user_phone = deps.get("user_phone", "")
        client_id = deps.get("client_id", "")
        if redis is None or not config.calendar_id:
            return []

        from app.integrations.padel_calendar import PadelCalendarClient
        from app.agent.padel_tools import build_padel_tools, build_padel_payment_tool

        padel = PadelCalendarClient(
            calendar_id=config.calendar_id,
            redis=redis,
            client_id=client_id or str(config.id),
        )

        tools = build_padel_tools(padel, config)

        if config.mp_access_token and user_phone:
            tools.append(build_padel_payment_tool(
                padel=padel,
                config=config,
                redis=redis,
                user_phone=user_phone,
                client_id=client_id,
            ))

        return tools
