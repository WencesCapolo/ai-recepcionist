"""
padel_tools.py — Anthropic tool factories for the padel court demo.

Tools:
  get_current_date_hour()
      Returns the current date and time in Argentina (ART, UTC-3).
      Call this first whenever the user refers to relative dates.

  get_availability(fecha, hora, cancha?)
      Shows which courts are free at the requested date and time.

  create_booking(fecha, hora, cancha, nombre, telefono)
      Validates the slot is free, books it, and returns a booking_id.

  cancel_booking(booking_id)
      Cancels a booking by its ID.

  get_price(tipo_turno)
      Returns the price for diurno / nocturno / fin de semana,
      extracted from the client's system prompt.

  get_hours()
      Returns business hours extracted from the client's system prompt.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta
from typing import Any

from app.clients.models import ClientConfig
from app.integrations.padel_calendar import PadelCalendar, PadelSlotError

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


def _normalise_time(time_str: str) -> str:
    """Accept '9:00' or '09:00', return normalised 'HH:MM'."""
    try:
        return datetime.strptime(time_str.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(
            f"El horario '{time_str}' no tiene el formato correcto. "
            "Usá HH:MM (por ejemplo, 09:00)."
        )


# ── get_current_date_hour ─────────────────────────────────────────────────────


def _make_get_current_date_hour() -> dict:
    def handler() -> str:
        now = datetime.now(_ART)
        day_names = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        month_names = [
            "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        return (
            f"Hoy es {day_names[now.weekday()]} {now.day} de {month_names[now.month]} de {now.year} "
            f"({now.strftime('%Y-%m-%d')}). "
            f"La hora actual en Argentina es {now.strftime('%H:%M')}."
        )

    return {
        "definition": {
            "name": "get_current_date_hour",
            "description": (
                "Devuelve la fecha y hora actual en Argentina (ART, UTC-3). "
                "Llamá a esta herramienta antes de usar get_availability o "
                "create_booking cuando el cliente use referencias relativas "
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


# ── build_padel_tools ─────────────────────────────────────────────────────────


def build_padel_tools(padel: PadelCalendar, config: ClientConfig) -> list[dict]:
    """
    Return all padel tool dicts ready to extend build_tools() output.
    """
    return [
        _make_get_current_date_hour(),
        _make_get_availability(padel),
        _make_create_booking(padel),
        _make_cancel_booking(padel),
        _make_get_price(config),
        _make_get_hours(config),
    ]


# ── get_availability ──────────────────────────────────────────────────────────


def _make_get_availability(padel: PadelCalendar) -> dict:
    def handler(fecha: str, hora: str, cancha: str | None = None) -> str:
        try:
            d = _parse_date(fecha)
        except ValueError as exc:
            return str(exc)
        try:
            time_norm = _normalise_time(hora)
        except ValueError as exc:
            return str(exc)

        today = datetime.now(_ART).date()
        if d < today:
            return "Esa fecha ya pasó. Elegí un día a partir de hoy."

        try:
            result = padel.get_availability(d, time_norm, cancha)
        except PadelSlotError as exc:
            return str(exc)

        if cancha is not None:
            resolved = result["cancha"]
            if result["available"]:
                return (
                    f"La {resolved} está libre el {_friendly_date(d)} a las {time_norm}. "
                    "La reservo?"
                )
            return (
                f"La {resolved} no está disponible a las {time_norm} el {_friendly_date(d)}. "
                "Querés que revise otra cancha u otro horario?"
            )

        free: list[str] = result["available"]
        if not free:
            return (
                f"No hay canchas disponibles el {_friendly_date(d)} a las {time_norm}. "
                "Querés que revise otro horario?"
            )
        courts_str = ", ".join(free)
        return (
            f"El {_friendly_date(d)} a las {time_norm} están libres: {courts_str}. "
            "Cuál preferís?"
        )

    return {
        "definition": {
            "name": "get_availability",
            "description": (
                "Muestra qué canchas están libres en una fecha y horario dados. "
                "Usá esta herramienta cuando el cliente quiera saber si hay lugar."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato AAAA-MM-DD (ej: 2025-03-15)",
                    },
                    "hora": {
                        "type": "string",
                        "description": "Horario en formato HH:MM (ej: '19:00')",
                    },
                    "cancha": {
                        "type": "string",
                        "description": (
                            "Nombre de la cancha (opcional): "
                            "'Cancha 1', 'Cancha 2' o 'Cancha 3'"
                        ),
                    },
                },
                "required": ["fecha", "hora"],
            },
        },
        "handler": handler,
    }


# ── create_booking ────────────────────────────────────────────────────────────


def _make_create_booking(padel: PadelCalendar) -> dict:
    def handler(
        fecha: str,
        hora: str,
        cancha: str,
        nombre: str,
        telefono: str,
    ) -> str:
        try:
            d = _parse_date(fecha)
        except ValueError as exc:
            return str(exc)
        try:
            time_norm = _normalise_time(hora)
        except ValueError as exc:
            return str(exc)

        today = datetime.now(_ART).date()
        if d < today:
            return "Esa fecha ya pasó. Elegí un día a partir de hoy."

        try:
            booking_id = padel.create_booking(
                d=d,
                time=time_norm,
                cancha=cancha.strip(),
                name=nombre.strip(),
                phone=telefono.strip(),
            )
        except PadelSlotError as exc:
            # Slot taken or invalid — show alternatives at that time
            try:
                avail = padel.get_availability(d, time_norm)
                free: list[str] = avail.get("available", [])
            except PadelSlotError:
                free = []

            msg = str(exc) + "\n\n"
            if free:
                msg += "Estas canchas siguen libres a esa hora: " + ", ".join(free) + ". Cuál te viene bien?"
            else:
                msg += "No quedan canchas libres a ese horario. Querés que revise otro?"
            return msg

        return (
            f"Reserva confirmada!\n\n"
            f"Cancha: {cancha.strip()}\n"
            f"Fecha: {_friendly_date(d)}\n"
            f"Horario: {time_norm}\n"
            f"Nombre: {nombre.strip()}\n"
            f"N° de reserva: #{booking_id}\n\n"
            "Guardá el número de reserva por si necesitás cancelar."
        )

    return {
        "definition": {
            "name": "create_booking",
            "description": (
                "Reserva una cancha de pádel. "
                "Antes de llamar a esta herramienta debés tener confirmados: "
                "fecha (AAAA-MM-DD), horario (HH:MM), cancha, nombre y teléfono del cliente."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "fecha": {
                        "type": "string",
                        "description": "Fecha en formato AAAA-MM-DD",
                    },
                    "hora": {
                        "type": "string",
                        "description": "Horario en formato HH:MM (ej: '19:00')",
                    },
                    "cancha": {
                        "type": "string",
                        "description": "'Cancha 1', 'Cancha 2' o 'Cancha 3'",
                    },
                    "nombre": {
                        "type": "string",
                        "description": "Nombre completo del cliente",
                    },
                    "telefono": {
                        "type": "string",
                        "description": "Teléfono de contacto",
                    },
                },
                "required": ["fecha", "hora", "cancha", "nombre", "telefono"],
            },
        },
        "handler": handler,
    }


# ── cancel_booking ────────────────────────────────────────────────────────────


def _make_cancel_booking(padel: PadelCalendar) -> dict:
    def handler(booking_id: str) -> str:
        try:
            found = padel.cancel_booking(booking_id.strip())
        except PadelSlotError as exc:
            return str(exc)

        if not found:
            return (
                f"No encontré una reserva con el número #{booking_id.strip()}. "
                "Verificá el número e intentá de nuevo."
            )
        return f"La reserva #{booking_id.strip()} fue cancelada correctamente."

    return {
        "definition": {
            "name": "cancel_booking",
            "description": (
                "Cancela una reserva de cancha usando el número de reserva. "
                "Pedile el número de reserva al cliente antes de llamar a esta herramienta."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "string",
                        "description": "Número de reserva recibido al confirmar el turno (ej: 'a3f2c1b0')",
                    },
                },
                "required": ["booking_id"],
            },
        },
        "handler": handler,
    }


# ── get_price ─────────────────────────────────────────────────────────────────


def _make_get_price(config: ClientConfig) -> dict:
    def handler(tipo_turno: str) -> str:
        query = tipo_turno.strip().lower()
        for line in config.system_prompt.splitlines():
            line_lower = line.strip().lower()
            if "precio" in line_lower and query in line_lower:
                return line.strip()
        return (
            f"No encontré el precio para '{tipo_turno}'. "
            "Consultá directamente con el complejo."
        )

    return {
        "definition": {
            "name": "get_price",
            "description": (
                "Devuelve el precio de un tipo de turno. "
                "Tipos válidos: 'diurno', 'nocturno', 'fin de semana'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "tipo_turno": {
                        "type": "string",
                        "description": "Tipo de turno: 'diurno', 'nocturno' o 'fin de semana'",
                    },
                },
                "required": ["tipo_turno"],
            },
        },
        "handler": handler,
    }


# ── get_hours ─────────────────────────────────────────────────────────────────


def _make_get_hours(config: ClientConfig) -> dict:
    def handler() -> str:
        for line in config.system_prompt.splitlines():
            if line.strip().lower().startswith("horario"):
                return line.strip()
        return "Consultá directamente con el complejo para conocer el horario."

    return {
        "definition": {
            "name": "get_hours",
            "description": "Devuelve el horario de atención del complejo.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        "handler": handler,
    }


# ── generate_padel_payment_link ───────────────────────────────────────────────


def build_padel_payment_tool(
    padel: PadelCalendar,
    config: ClientConfig,
    redis: Any,
    user_phone: str,
    client_id: str,
) -> dict:
    """
    Build the generate_padel_payment_link tool.
    Separated from build_padel_tools because it requires MP credentials.
    """
    import json as _json
    from app.integrations.mercadopago import MercadoPagoClient, MercadoPagoError

    MP_PAYMENT_TTL = 86400
    mp = MercadoPagoClient(
        access_token=config.mp_access_token,
        sandbox=config.mp_sandbox,
    )

    async def handler(booking_id: str, monto: float) -> str:
        booking_key = f"padel:booking:{client_id}:{booking_id.strip()}"
        try:
            raw = redis.get(booking_key)
        except Exception:
            raw = None

        if not raw:
            return (
                f"No encontré la reserva #{booking_id.strip()}. "
                "Verificá el número e intentá de nuevo."
            )

        meta = _json.loads(raw)
        title = (
            f"Cancha de Pádel – {meta['cancha']} – "
            f"{meta['date']} {meta['time']}"
        )

        try:
            preference = await mp.create_payment_link(
                title=title,
                unit_price=float(monto),
                quantity=1,
            )
        except MercadoPagoError:
            return "No pude generar el link de pago. Intentá más tarde o coordiná el pago en el complejo."

        payment_meta = _json.dumps({
            "user_phone": user_phone,
            "client_id": client_id,
            "product": title,
            "quantity": 1,
            "unit_price": float(monto),
            "total": float(monto),
            "booking_id": booking_id.strip(),
        })
        redis.set(
            f"mp_payment:{preference.preference_id}",
            payment_meta,
            ex=MP_PAYMENT_TTL,
        )

        return (
            f"Acá tenés el link para pagar la reserva de {meta['cancha']} "
            f"el {meta['date']} a las {meta['time']} "
            f"(total: ${monto:,.0f} ARS):\n{preference.init_point}\n"
            "Una vez que pagues, tu reserva queda asegurada."
        )

    return {
        "definition": {
            "name": "generate_padel_payment_link",
            "description": (
                "Genera un link de pago de MercadoPago para una reserva de cancha. "
                "Usá esta herramienta SOLO después de confirmar la reserva y "
                "si el cliente quiere pagar online. "
                "Necesitás el booking_id y el monto a cobrar."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "booking_id": {
                        "type": "string",
                        "description": "Número de reserva obtenido con create_booking",
                    },
                    "monto": {
                        "type": "number",
                        "description": "Monto a cobrar en ARS",
                    },
                },
                "required": ["booking_id", "monto"],
            },
        },
        "handler": handler,
    }
