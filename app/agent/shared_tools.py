"""
app/agent/shared_tools.py

Tool factories shared across verticals.

- make_get_current_date_hour() — current Argentina date/time, no config required.
- make_get_hours(config)       — business hours from system_prompt, no config required.

These are registered once in build_tools_for_client regardless of which vertical
the client belongs to.
"""

from __future__ import annotations

import logfire
from datetime import datetime

from app.clients.models import ClientConfig
from app.integrations.argentina import ART, DAYS_ES, MONTHS_ES


def make_get_current_date_hour() -> dict:
    async def handler() -> str:
        with logfire.span("tool.get_current_date_hour"):
            now = datetime.now(ART)
            return (
                f"Hoy es {DAYS_ES[now.weekday()]} {now.day} de "
                f"{MONTHS_ES[now.month]} de {now.year} "
                f"({now.strftime('%Y-%m-%d')}). "
                f"La hora actual en Argentina es {now.strftime('%H:%M')}."
            )

    return {
        "definition": {
            "name": "get_current_date_hour",
            "description": (
                "Devuelve la fecha y hora actual en Argentina (ART, UTC-3). "
                "Llamá a esta herramienta antes de usar check_availability, "
                "get_availability o create_booking cuando el cliente use referencias "
                "relativas como 'hoy', 'mañana', 'esta semana' o 'lo más pronto posible'."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        "handler": handler,
    }


def make_get_hours(config: ClientConfig) -> dict:
    async def handler() -> str:
        with logfire.span("tool.get_hours", client_id=str(config.id)):
            for line in config.system_prompt.splitlines():
                if line.strip().lower().startswith("horario"):
                    return line.strip()
            return "Consultá directamente con el local para conocer el horario."

    return {
        "definition": {
            "name": "get_hours",
            "description": "Devuelve el horario de atención del local o complejo.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        "handler": handler,
    }
