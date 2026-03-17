"""
app/agent/calendar_tools.py

Anthropic tool factories for Google Calendar integration.

Six tools:
  get_current_date_hour()           — current date/time in Argentina
  check_availability(date)          — free slots for a given date
  book_appointment(...)             — create a new appointment
  get_appointment(patient_name)     — look up upcoming appointments by name
  cancel_appointment(...)           — cancel an existing appointment
  reschedule_appointment(...)       — move an existing appointment to a new slot

All tool handlers are synchronous (googleapiclient is sync).
No LangChain — native Anthropic function calling format only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_ART = timezone(timedelta(hours=-3))

_MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_DAYS_ES = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
]


def _parse_date(date_str: str) -> date:
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(
            f"La fecha '{date_str}' no tiene el formato correcto. "
            "Usá el formato AAAA-MM-DD (por ejemplo, 2025-03-15)."
        )


def _friendly_date(d: date) -> str:
    return f"{_DAYS_ES[d.weekday()]} {d.day} de {_MONTHS_ES[d.month]} de {d.year}"


# ── Entry point ───────────────────────────────────────────────────────────────


def build_calendar_tools(calendar: Any) -> list[dict]:
    """
    Return all calendar tool dicts ready to extend build_tools() output.

    ``calendar`` must be a GoogleCalendarClient (or CalendarMock for tests).
    The list is indexed by tool name in tools.py's build_tools().
    """
    return [
        _make_get_current_date_hour(),
        _make_check_availability(calendar),
        _make_book_appointment(calendar),
        _make_get_appointment(calendar),
        _make_cancel_appointment(calendar),
        _make_reschedule_appointment(calendar),
    ]


# ── get_current_date_hour ─────────────────────────────────────────────────────


def _make_get_current_date_hour() -> dict:
    def handler() -> str:
        now = datetime.now(_ART)
        day_name   = _DAYS_ES[now.weekday()]
        month_name = _MONTHS_ES[now.month]
        return (
            f"Hoy es {day_name} {now.day} de {month_name} de {now.year} "
            f"({now.strftime('%Y-%m-%d')}). "
            f"La hora actual en Argentina es {now.strftime('%H:%M')}."
        )

    return {
        "definition": {
            "name": "get_current_date_hour",
            "description": (
                "Devuelve la fecha y hora actual en Argentina (ART, UTC-3). "
                "Llamá a esta herramienta antes de usar check_availability o "
                "book_appointment cuando el paciente use referencias relativas "
                "como 'hoy', 'mañana', 'esta semana' o 'lo más pronto posible'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        "handler": handler,
    }


# ── check_availability ────────────────────────────────────────────────────────


def _make_check_availability(calendar: Any) -> dict:
    def handler(date: str) -> str:  # noqa: A002
        try:
            d = _parse_date(date)
        except ValueError as exc:
            return str(exc)

        today = datetime.now(_ART).date()
        if d < today:
            return "Esa fecha ya pasó. Elegí un día a partir de hoy."
        if d.weekday() >= 5:
            return "No trabajamos los fines de semana. Elegí un día de lunes a viernes."

        try:
            slots = calendar.available_slots(d)
        except Exception:
            logger.exception("check_availability failed for %s", date)
            return "No pude consultar la disponibilidad en este momento. Intentá de nuevo."

        morning   = slots["morning"]
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
        date: str,          # noqa: A002
        time: str,
        is_new_patient: bool = True,
        conversation_id: str = "",
    ) -> str:
        try:
            d = _parse_date(date)
        except ValueError as exc:
            return str(exc)

        today = datetime.now(_ART).date()
        if d < today:
            return "Esa fecha ya pasó. Elegí un día a partir de hoy."
        if d.weekday() >= 5:
            return "No trabajamos los fines de semana. Elegí un día de lunes a viernes."

        try:
            time_obj  = datetime.strptime(time.strip(), "%H:%M")
            time_norm = time_obj.strftime("%H:%M")
        except ValueError:
            return (
                f"El horario '{time}' no tiene el formato correcto. "
                "Usá HH:MM (por ejemplo, 09:30)."
            )

        from app.integrations.calendar import CalendarSlotError

        try:
            event_id = calendar.book(
                d=d,
                time=time_norm,
                name=name.strip(),
                phone=phone.strip(),
                reason=reason.strip(),
                conversation_id=conversation_id,
                is_new_patient=is_new_patient,
            )
        except CalendarSlotError as exc:
            return str(exc)
        except Exception:
            logger.exception("book_appointment unexpected error [name=%s date=%s]", name, date)
            return "No se pudo confirmar el turno por un error interno. Intentá de nuevo."

        return (
            f"✅ ¡Turno confirmado!\n\n"
            f"📋 *Paciente:* {name}\n"
            f"📅 *Fecha:* {_friendly_date(d)}\n"
            f"🕐 *Horario:* {time_norm}\n"
            f"🦷 *Motivo:* {reason}\n\n"
            f"ID del turno: {event_id}\n\n"
            "Te esperamos. Si necesitás cancelar o cambiar el turno, avisanos con anticipación."
        )

    return {
        "definition": {
            "name": "book_appointment",
            "description": (
                "Reserva un turno en el consultorio en Google Calendar. "
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
                    "is_new_patient": {
                        "type": "boolean",
                        "description": "True si es la primera vez que viene al consultorio",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "ID de conversación (para el lock de Redis)",
                    },
                },
                "required": ["name", "phone", "reason", "date", "time"],
            },
        },
        "handler": handler,
    }


# ── get_appointment ───────────────────────────────────────────────────────────


def _make_get_appointment(calendar: Any) -> dict:
    def handler(patient_name: str) -> str:
        try:
            appointments = calendar.find_by_name(patient_name)
        except Exception:
            logger.exception("get_appointment failed for %s", patient_name)
            return "No pude consultar los turnos en este momento. Intentá de nuevo."

        if not appointments:
            return f"No encontré turnos próximos para {patient_name}."

        lines = []
        for appt in appointments:
            start = appt["start_dt"].astimezone(_ART)
            d = start.date()
            time_str = start.strftime("%H:%M")
            lines.append(
                f"- {_friendly_date(d)} a las {time_str}: "
                f"{appt['summary']}  [ID: {appt['event_id']}]"
            )

        return f"Turnos encontrados para {patient_name}:\n" + "\n".join(lines)

    return {
        "definition": {
            "name": "get_appointment",
            "description": (
                "Busca los próximos turnos de un paciente por nombre. "
                "Usá esta herramienta antes de cancelar o reprogramar un turno."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "patient_name": {
                        "type": "string",
                        "description": "Nombre completo (o parcial) del paciente",
                    }
                },
                "required": ["patient_name"],
            },
        },
        "handler": handler,
    }


# ── cancel_appointment ────────────────────────────────────────────────────────


def _make_cancel_appointment(calendar: Any) -> dict:
    def handler(event_id: str, patient_name: str) -> str:
        from app.integrations.calendar import CalendarSlotError

        try:
            return calendar.cancel(event_id=event_id, patient_name=patient_name)
        except CalendarSlotError as exc:
            return str(exc)
        except Exception:
            logger.exception("cancel_appointment failed [event_id=%s]", event_id)
            return "No se pudo cancelar el turno por un error interno. Intentá de nuevo."

    return {
        "definition": {
            "name": "cancel_appointment",
            "description": (
                "Cancela un turno dado su event_id de Google Calendar. "
                "Confirmá con el paciente antes de llamar esta herramienta. "
                "Usá get_appointment primero para obtener el event_id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID del evento de Google Calendar",
                    },
                    "patient_name": {
                        "type": "string",
                        "description": "Nombre del paciente para confirmar que el turno le corresponde",
                    },
                },
                "required": ["event_id", "patient_name"],
            },
        },
        "handler": handler,
    }


# ── reschedule_appointment ────────────────────────────────────────────────────


def _make_reschedule_appointment(calendar: Any) -> dict:
    def handler(
        event_id: str,
        new_date: str,
        new_time: str,
        conversation_id: str = "",
    ) -> str:
        try:
            d = _parse_date(new_date)
        except ValueError as exc:
            return str(exc)

        today = datetime.now(_ART).date()
        if d < today:
            return "Esa fecha ya pasó. Elegí un día a partir de hoy."

        try:
            time_obj  = datetime.strptime(new_time.strip(), "%H:%M")
            time_norm = time_obj.strftime("%H:%M")
        except ValueError:
            return (
                f"El horario '{new_time}' no tiene el formato correcto. "
                "Usá HH:MM (por ejemplo, 10:00)."
            )

        from app.integrations.calendar import CalendarSlotError

        try:
            return calendar.reschedule(
                event_id=event_id,
                new_date=d,
                new_time=time_norm,
                conversation_id=conversation_id,
            )
        except CalendarSlotError as exc:
            return str(exc)
        except Exception:
            logger.exception("reschedule_appointment failed [event_id=%s]", event_id)
            return "No se pudo reprogramar el turno por un error interno. Intentá de nuevo."

    return {
        "definition": {
            "name": "reschedule_appointment",
            "description": (
                "Reprograma un turno existente a una nueva fecha y horario. "
                "Primero llamá check_availability para mostrar slots disponibles, "
                "luego llamá esta herramienta con el nuevo horario elegido por el paciente. "
                "Usá get_appointment primero para obtener el event_id."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID del evento a reprogramar",
                    },
                    "new_date": {
                        "type": "string",
                        "description": "Nueva fecha en formato AAAA-MM-DD",
                    },
                    "new_time": {
                        "type": "string",
                        "description": "Nuevo horario en formato HH:MM (ej: '14:00')",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "ID de conversación (para el lock de Redis)",
                    },
                },
                "required": ["event_id", "new_date", "new_time"],
            },
        },
        "handler": handler,
    }
