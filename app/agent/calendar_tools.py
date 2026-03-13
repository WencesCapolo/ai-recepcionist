"""
calendar_tools.py — Anthropic tool factories for the dentist-demo calendar.

Two tools:
  check_availability(date)
      Shows free morning/afternoon slots for the requested date.

  book_appointment(name, phone, reason, date, time)
      Validates the slot is free, then stores the booking in Redis.
      The bot must collect all five fields conversationally before calling this.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Spanish month names for friendly output
_MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_DAYS_ES = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
]


def _parse_date(date_str: str) -> date:
    """
    Accept YYYY-MM-DD.  Raises ValueError with a Spanish message on bad input.
    """
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(
            f"La fecha '{date_str}' no tiene el formato correcto. "
            "Usá el formato AAAA-MM-DD (por ejemplo, 2025-03-15)."
        )


def _friendly_date(d: date) -> str:
    return f"{_DAYS_ES[d.weekday()]} {d.day} de {_MONTHS_ES[d.month]} de {d.year}"


# ── build_calendar_tools ──────────────────────────────────────────────────────


def build_calendar_tools(calendar: Any) -> list[dict]:
    """
    Return the two calendar tool dicts ready to extend build_tools() output.

    ``calendar`` must be a CalendarMock instance (injected by build_tools).
    """
    return [
        _make_check_availability(calendar),
        _make_book_appointment(calendar),
    ]


# ── check_availability ────────────────────────────────────────────────────────


def _make_check_availability(calendar: Any) -> dict:
    def handler(date: str) -> str:  # noqa: A002
        try:
            d = _parse_date(date)
        except ValueError as exc:
            return str(exc)

        today = datetime.now().date()
        if d < today:
            return "Esa fecha ya pasó. Elegí un día a partir de hoy."
        if d.weekday() >= 5:
            return "No trabajamos los fines de semana. Elegí un día de lunes a viernes."

        slots = calendar.available_slots(d)
        morning = slots["morning"]
        afternoon = slots["afternoon"]

        if not morning and not afternoon:
            return (
                f"No hay turnos disponibles el {_friendly_date(d)}. "
                "¿Querés que te muestre otra fecha?"
            )

        friendly = f"📅 Turnos disponibles para el {_friendly_date(d)}:\n\n"
        if morning:
            friendly += "🌅 *Mañana:* " + "  •  ".join(morning) + "\n"
        if afternoon:
            friendly += "🌇 *Tarde:* " + "  •  ".join(afternoon) + "\n"
        friendly += "\n¿Cuál horario te viene mejor?"
        return friendly

    return {
        "definition": {
            "name": "check_availability",
            "description": (
                "Muestra los turnos libres para una fecha dada. "
                "Usá esta herramienta cuando el paciente quiera saber qué días/horarios están disponibles. "
                "Devuelve los turnos agrupados en mañana y tarde."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Fecha en formato AAAA-MM-DD (por ejemplo, 2025-03-15)",
                    }
                },
                "required": ["date"],
            },
        },
        "handler": handler,
    }


# ── book_appointment ──────────────────────────────────────────────────────────


def _make_book_appointment(calendar: Any) -> dict:
    def handler(
        name: str,
        phone: str,
        reason: str,
        date: str,  # noqa: A002
        time: str,
    ) -> str:
        # 1. Parse date
        try:
            d = _parse_date(date)
        except ValueError as exc:
            return str(exc)

        today = datetime.now().date()
        if d < today:
            return "Esa fecha ya pasó. Elegí un día a partir de hoy."
        if d.weekday() >= 5:
            return "No trabajamos los fines de semana. Elegí un día de lunes a viernes."

        # 2. Normalise time — accept "9:00" or "09:00"
        try:
            time_obj = datetime.strptime(time.strip(), "%H:%M")
            time_norm = time_obj.strftime("%H:%M")
        except ValueError:
            return (
                f"El horario '{time}' no tiene el formato correcto. "
                "Usá HH:MM (por ejemplo, 09:30)."
            )

        # 3. Attempt booking via CalendarMock
        from app.integrations.calendar_mock import CalendarSlotError

        try:
            booked = calendar.book(
                d=d,
                time=time_norm,
                name=name.strip(),
                phone=phone.strip(),
                reason=reason.strip(),
            )
        except CalendarSlotError as exc:
            return str(exc)

        if not booked:
            # Slot was taken — show what's still free
            slots = calendar.available_slots(d)
            morning = slots["morning"]
            afternoon = slots["afternoon"]

            msg = (
                f"Lo siento, el turno de las {time_norm} el {_friendly_date(d)} "
                "ya fue tomado.\n\n"
            )
            if morning or afternoon:
                msg += "Estos horarios siguen disponibles:\n"
                if morning:
                    msg += "🌅 *Mañana:* " + "  •  ".join(morning) + "\n"
                if afternoon:
                    msg += "🌇 *Tarde:* " + "  •  ".join(afternoon) + "\n"
                msg += "\n¿Cuál preferís?"
            else:
                msg += "Lamentablemente no quedan turnos para ese día. ¿Probamos con otra fecha?"
            return msg

        # 4. Confirm booking
        return (
            f"✅ ¡Turno confirmado!\n\n"
            f"📋 *Paciente:* {name}\n"
            f"📅 *Fecha:* {_friendly_date(d)}\n"
            f"🕐 *Horario:* {time_norm}\n"
            f"🦷 *Motivo:* {reason}\n\n"
            "Te esperamos. Si necesitás cancelar o cambiar el turno, avisanos con anticipación."
        )

    return {
        "definition": {
            "name": "book_appointment",
            "description": (
                "Reserva un turno en el consultorio. "
                "Antes de llamar a esta herramienta debés tener confirmados: "
                "nombre completo del paciente, teléfono, motivo de la consulta, "
                "fecha (AAAA-MM-DD) y horario (HH:MM). "
                "Recolectalos conversacionalmente, uno a uno si es necesario."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nombre completo del paciente",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Teléfono de contacto del paciente",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Motivo de la consulta (ej: 'limpieza dental', 'dolor de muela')",
                    },
                    "date": {
                        "type": "string",
                        "description": "Fecha del turno en formato AAAA-MM-DD",
                    },
                    "time": {
                        "type": "string",
                        "description": "Horario del turno en formato HH:MM (ej: '09:30')",
                    },
                },
                "required": ["name", "phone", "reason", "date", "time"],
            },
        },
        "handler": handler,
    }
