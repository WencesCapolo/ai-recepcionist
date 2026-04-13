from typing import Any

from app.clients.models import ClientConfig


def build_padel_tools(
    config: ClientConfig,
    *,
    redis: Any = None,
    user_phone: str = "",
    client_id: str = "",
    **_: Any,
) -> list[dict]:
    cfg = config.tool_config.padel
    if cfg is None or redis is None:
        return []

    from app.integrations.padel_calendar import PadelCalendarClient
    from app.agent.padel_tools import build_padel_tools as _build, build_padel_payment_tool

    padel = PadelCalendarClient(
        calendar_id=cfg.calendar_id,
        redis=redis,
        client_id=client_id or str(config.id),
    )

    tools = _build(padel, config)

    if config.tool_config.payment and user_phone:
        tools.append(build_padel_payment_tool(
            padel=padel,
            config=config,
            redis=redis,
            user_phone=user_phone,
            client_id=client_id or str(config.id),
        ))

    return tools
