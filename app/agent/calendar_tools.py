"""
app/agent/calendar_tools.py

Calendar tools in native Anthropic function-calling format.
Mirrors the dict pattern used in tools.py: each tool is
{"definition": {...}, "handler": callable}.

build_calendar_tools(calendar) is called from tools.py when
calendar tool names appear in config.tools_enabled.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.integrations.calendar import GoogleCalendarClient
    from app.integrations.calendar_mock import CalendarMock

logger = logging.getLogger(__name__)


def build_calendar_tools(calendar) -> list[dict]:
    """
    Returns a list of tool dicts (definition + handler) for the given
    calendar backend (GoogleCalendarClient or CalendarMock).
    """
    return [
        _make_get_current_date_hour(calendar),
        _make_get_treatment_info(calendar),
        _make_check_availability(calendar),
        _make_book_appointment(calendar),
        _make_get_appointment(calendar),
        _make_cancel_appointment(calendar),
        _make_reschedule_appointment(calendar),
    ]


# ---------------------------------------------------------------------------
# get_current_date_hour
# ---------------------------------------------------------------------------

def _make_get_current_date_hour(calendar) -> dict:
    def handler() -> str:
        return calendar.get_current_date_hour()

    return {
        "definition": {
            "name": "get_current_date_hour",
            "description": (
                "Devuelve la fecha y hora actual en Argentina (ART, UTC-3). "
                "Llamar SIEMPRE antes de responder preguntas sobre qué día es hoy, "
                "qué hora es, o para calcular fechas relativas como 'mañana' o 'la semana que viene'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        "handler": handler,
    }


# ---------------------------------------------------------------------------
# check_availability
# ---------------------------------------------------------------------------

def _make_check_availability(calendar) -> dict:
    def handler(count: int = 3, duration_minutes: int = 30) -> str:
        return calendar.check_availability(count=min(count, 5), duration_minutes=duration_minutes)

    return {
        "definition": {
            "name": "check_availability",
            "description": (
                "Devuelve los próximos turnos disponibles en el consultorio. "
                "Llamar SIEMPRE antes de ofrecer horarios al paciente. "
                "NUNCA ofrecer horarios sin llamar esta herramienta primero. "
                "Si ya llamaste get_treatment_info, pasá el duration_minutes obtenido."
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
                },
                "required": [],
            },
        },
        "handler": handler,
    }


# ---------------------------------------------------------------------------
# book_appointment
# ---------------------------------------------------------------------------

def _make_book_appointment(calendar) -> dict:
    def handler(
        patient_name: str,
        patient_phone: str,
        patient_email: str,
        reason: str,
        slot_iso: str,
        is_new_patient: bool = True,
        duration_minutes: int = 30,
    ) -> str:
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
                    "patient_name": {
                        "type": "string",
                        "description": "Nombre completo del paciente",
                    },
                    "patient_phone": {
                        "type": "string",
                        "description": "Número de WhatsApp del paciente (formato internacional)",
                    },
                    "patient_email": {
                        "type": "string",
                        "description": "Email del paciente para enviar confirmación y recordatorio",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Motivo de la consulta (ej: limpieza, extracción, control)",
                    },
                    "slot_iso": {
                        "type": "string",
                        "description": (
                            "Slot elegido en formato ISO 8601 con timezone, "
                            "tal como lo devolvió check_availability"
                        ),
                    },
                    "is_new_patient": {
                        "type": "boolean",
                        "description": "True si es la primera vez que viene al consultorio",
                        "default": True,
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Duración del turno en minutos (obtenido de get_treatment_info). Default 30.",
                        "default": 30,
                    },
                },
                "required": [
                    "patient_name",
                    "patient_phone",
                    "patient_email",
                    "reason",
                    "slot_iso",
                ],
            },
        },
        "handler": handler,
    }


# ---------------------------------------------------------------------------
# get_appointment
# ---------------------------------------------------------------------------

def _make_get_appointment(calendar) -> dict:
    def handler(patient_name: str, date_hint: str = "") -> str:
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
                    "patient_name": {
                        "type": "string",
                        "description": "Nombre completo del paciente",
                    },
                    "date_hint": {
                        "type": "string",
                        "description": "Fecha aproximada del turno (ej: '20 de marzo', '2026-03-20'). Opcional.",
                    },
                },
                "required": ["patient_name"],
            },
        },
        "handler": handler,
    }


# ---------------------------------------------------------------------------
# cancel_appointment
# ---------------------------------------------------------------------------

def _make_cancel_appointment(calendar) -> dict:
    def handler(event_id: str, patient_name: str) -> str:
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
                    "event_id": {
                        "type": "string",
                        "description": "ID del evento, obtenido de get_appointment",
                    },
                    "patient_name": {
                        "type": "string",
                        "description": "Nombre del paciente para verificar antes de cancelar",
                    },
                },
                "required": ["event_id", "patient_name"],
            },
        },
        "handler": handler,
    }


# ---------------------------------------------------------------------------
# reschedule_appointment
# ---------------------------------------------------------------------------

def _make_reschedule_appointment(calendar) -> dict:
    def handler(event_id: str, new_slot_iso: str) -> str:
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
                    "event_id": {
                        "type": "string",
                        "description": "ID del evento a reprogramar, obtenido de get_appointment",
                    },
                    "new_slot_iso": {
                        "type": "string",
                        "description": "Nuevo slot en formato ISO 8601, obtenido de check_availability",
                    },
                },
                "required": ["event_id", "new_slot_iso"],
            },
        },
        "handler": handler,
    }


# ---------------------------------------------------------------------------
# get_treatment_info
# ---------------------------------------------------------------------------

def _make_get_treatment_info(calendar) -> dict:
    def handler(treatment: str) -> str:
        return calendar.get_treatment_info(treatment)

    return {
        "definition": {
            "name": "get_treatment_info",
            "description": (
                "Devuelve la duración en minutos y el precio de un tratamiento odontológico. "
                "Llamar ANTES de check_availability cuando el paciente dice el motivo de consulta, "
                "para pasar el duration_minutes correcto a check_availability y book_appointment."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "treatment": {
                        "type": "string",
                        "description": "Nombre del tratamiento (ej: 'limpieza', 'extracción', 'conducto')",
                    }
                },
                "required": ["treatment"],
            },
        },
        "handler": handler,
    }