from typing import Any

from app.clients.models import ClientConfig


def build_calendar_tools(
    config: ClientConfig,
    *,
    redis: Any = None,
    client_id: str = "",
    **_: Any,
) -> list[dict]:
    cfg = config.tool_config.calendar
    if cfg is None or redis is None:
        return []

    if cfg.calendar_id:
        from app.integrations.calendar import GoogleCalendarClient
        calendar = GoogleCalendarClient(
            calendar_id=cfg.calendar_id,
            redis=redis,
            client_id=client_id or str(config.id),
            slot_minutes=cfg.slot_minutes,
            work_start=cfg.work_start,
            work_end=cfg.work_end,
            work_days=cfg.work_days,
        )
    else:
        from app.integrations.calendar_mock import CalendarMock
        calendar = CalendarMock(
            redis=redis,
            client_id=client_id or str(config.id),
            slot_minutes=cfg.slot_minutes,
            work_start=cfg.work_start,
            work_end=cfg.work_end,
            work_days=cfg.work_days,
        )

    from app.agent.calendar_tools import build_calendar_tools as _build
    return _build(calendar)
