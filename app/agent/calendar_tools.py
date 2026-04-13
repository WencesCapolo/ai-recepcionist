"""
app/agent/calendar_tools.py

Calendar tool factories (check_availability, book_appointment, etc.).
Used by verticals/calendar_booking.py.

get_current_date_hour lives in shared_tools.py — not here.
"""

from __future__ import annotations

import logging

import logfire

logger = logging.getLogger(__name__)


def build_calendar_tools(calendar) -> list[dict]:
    """
    Returns tool dicts (definition + handler) for the given calendar backend
    (GoogleCalendarClient or CalendarMock).
    """
    return [
        _make_check_availability(calendar),
        _make_book_appointment(calendar),
        _make_get_appointment(calendar),
        _make_cancel_appointment(calendar),
        _make_reschedule_appointment(calendar),
    ]


def _make_check_availability(calendar) -> dict:
    async def handler(count: int = 3, duration_minutes: int = 30, after_hour: int = 0, before_hour: int = 24) -> str:
        with logfire.span("tool.check_availability"):
            return calendar.check_availability(
                count=min(count, 5),
                duration_minutes=duration_minutes,
                after_hour=after_hour,
                before_hour=before_hour,
            )

    return {
        "definition": {
            "name": "check_availability",
            "description": (
                "Devuelve los próximos turnos disponibles en el consultorio. "
                "Llamar SIEMPRE antes de ofrecer horarios al paciente. "
                "NUNCA ofrecer horarios sin llamar esta herramienta primero. "
                "Si ya llamaste get_treatment_info, pasá el duration_minutes obtenido. "
                "Usá after_hour=13 si el paciente pide turno de tarde, after_hour=8 before_hour=12 para mañana."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Cantidad de turnos a mostrar (default 3, max 5)",
                        "default": 3,
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Duración del turno en minutos (obtenido de get_treatment_info). Default 30.",
                        "default": 30,
                    },
                    "after_hour": {
                        "type": "integer",
                        "description": "Hora mínima del turno en formato 24h. Usá 13 para turnos de tarde, 0 para cualquier hora.",
                        "default": 0,
                    },
                    "before_hour": {
                        "type": "integer",
                        "description": "Hora máxima del turno en formato 24h. Usá 12 para turnos de mañana, 24 para cualquier hora.",
                        "default": 24,
                    },
                },
                "required": [],
            },
        },
        "handler": handler,
    }


def _make_book_appointment(calendar) -> dict:
    async def handler(
        patient_name: str,
        patient_phone: str,
        patient_email: str,
        reason: str,
        slot_iso: str,
        is_new_patient: bool = True,
        duration_minutes: int = 30,
    ) -> str:
        with logfire.span("tool.book_appointment"):
            return calendar.book_appointment(
                patient_name=patient_name,
                patient_phone=patient_phone,
                patient_email=patient_email,
                reason=reason,
                slot_iso=slot_iso,
                is_new_patient=is_new_patient,
                duration_minutes=duration_minutes,
            )

    return {
        "definition": {
            "name": "book_appointment",
            "description": (
                "Crea un turno en el calendario del consultorio. "
                "Llamar SOLO cuando el paciente confirmó: nombre completo, motivo, "
                "el slot elegido (slot_iso del resultado de check_availability) y su email."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "patient_name":    {"type": "string", "description": "Nombre completo del paciente"},
                    "patient_phone":   {"type": "string", "description": "Número de WhatsApp del paciente (formato internacional)"},
                    "patient_email":   {"type": "string", "description": "Email del paciente para enviar confirmación y recordatorio"},
                    "reason":          {"type": "string", "description": "Motivo de la consulta (ej: limpieza, extracción, control)"},
                    "slot_iso":        {
                        "type": "string",
                        "description": "Slot elegido en formato ISO 8601 con timezone, tal como lo devolvió check_availability",
                    },
                    "is_new_patient":  {"type": "boolean", "description": "True si es la primera vez que viene al consultorio", "default": True},
                    "duration_minutes": {"type": "integer", "description": "Duración del turno en minutos (obtenido de get_treatment_info). Default 30.", "default": 30},
                },
                "required": ["patient_name", "patient_phone", "patient_email", "reason", "slot_iso"],
            },
        },
        "handler": handler,
    }


def _make_get_appointment(calendar) -> dict:
    async def handler(patient_name: str, date_hint: str = "") -> str:
        with logfire.span("tool.get_appointment"):
            return calendar.get_appointment(
                patient_name=patient_name,
                date_hint=date_hint or None,
            )

    return {
        "definition": {
            "name": "get_appointment",
            "description": (
                "Busca los turnos próximos de un paciente por nombre. "
                "Usar para cancelar o reprogramar. Devuelve event_id necesario "
                "para cancel_appointment y reschedule_appointment."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string", "description": "Nombre completo del paciente"},
                    "date_hint":    {"type": "string", "description": "Fecha aproximada del turno (ej: '20 de marzo', '2026-03-20'). Opcional."},
                },
                "required": ["patient_name"],
            },
        },
        "handler": handler,
    }


def _make_cancel_appointment(calendar) -> dict:
    async def handler(event_id: str, patient_name: str) -> str:
        with logfire.span("tool.cancel_appointment"):
            return calendar.cancel_appointment(
                event_id=event_id,
                patient_name=patient_name,
            )

    return {
        "definition": {
            "name": "cancel_appointment",
            "description": (
                "Cancela un turno dado su event_id. "
                "Confirmar con el paciente el turno encontrado ANTES de llamar esta herramienta."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_id":     {"type": "string", "description": "ID del evento, obtenido de get_appointment"},
                    "patient_name": {"type": "string", "description": "Nombre del paciente para verificar antes de cancelar"},
                },
                "required": ["event_id", "patient_name"],
            },
        },
        "handler": handler,
    }


def _make_reschedule_appointment(calendar) -> dict:
    async def handler(event_id: str, new_slot_iso: str) -> str:
        with logfire.span("tool.reschedule_appointment"):
            return calendar.reschedule_appointment(
                event_id=event_id,
                new_slot_iso=new_slot_iso,
            )

    return {
        "definition": {
            "name": "reschedule_appointment",
            "description": (
                "Reprograma un turno existente a un nuevo horario. "
                "Primero llamar check_availability para mostrar opciones, "
                "luego llamar esta herramienta con el slot elegido por el paciente."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_id":     {"type": "string", "description": "ID del evento a reprogramar, obtenido de get_appointment"},
                    "new_slot_iso": {"type": "string", "description": "Nuevo slot en formato ISO 8601, obtenido de check_availability"},
                },
                "required": ["event_id", "new_slot_iso"],
            },
        },
        "handler": handler,
    }
