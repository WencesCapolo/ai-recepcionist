from typing import Any
from app.clients.models import ClientConfig
from app.agent.registry import CALENDAR_TOOLS

class CalendarBookingToolset:
    name = "calendar_booking"
    required_tools = frozenset(t.value for t in CALENDAR_TOOLS)

    def is_applicable(self, config: ClientConfig) -> bool:
        enabled = frozenset(config.tools_enabled)
        return bool(enabled & self.required_tools)

    def build(self, config: ClientConfig, **deps: Any) -> list[dict]:
        redis = deps.get("redis")
        client_id = deps.get("client_id", "")
        if redis is None:
            return []

        from app.agent.calendar_tools import build_calendar_tools
        if config.calendar_id:
            from app.integrations.calendar import GoogleCalendarClient
            _calendar = GoogleCalendarClient(
                calendar_id=config.calendar_id,
                redis=redis,
                client_id=client_id or str(config.id),
                slot_minutes=config.slot_minutes,
                work_start=config.work_start_hour,
                work_end=config.work_end_hour,
                work_days=config.work_days,
            )
        else:
            from app.integrations.calendar_mock import CalendarMock
            _calendar = CalendarMock(
                redis=redis,
                client_id=client_id or str(config.id),
                slot_minutes=config.slot_minutes,
                work_start=config.work_start_hour,
                work_end=config.work_end_hour,
                work_days=config.work_days,
            )

        return build_calendar_tools(_calendar)
