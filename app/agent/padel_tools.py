"""
app/agent/padel_tools.py

Padel court tool factories.

Tools:
  get_availability(fecha, hora, cancha?)  — free courts at a given date/time.
  create_booking(fecha, hora, cancha, nombre, telefono) — book a court.
  cancel_booking(booking_id)              — cancel by booking ID.
  get_price(product)                      — court pricing from system prompt.
  generate_padel_payment_link(booking_id, monto) — MercadoPago link for a booking.

get_current_date_hour and get_hours are shared tools (shared_tools.py) — not here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import logfire

from app.clients.models import ClientConfig, PadelConfig, PaymentConfig
from app.integrations.padel_calendar import PadelCalendarClient, PadelCalendarError
from app.integrations.argentina import ART, fmt_date as _friendly_date

logger = logging.getLogger(__name__)


def _parse_date(date_str: str) -> date:
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(
            f"La fecha '{date_str}' no tiene el formato correcto. "
            "Usá el formato AAAA-MM-DD (por ejemplo, 2025-03-15)."
        )


def _normalise_time(time_str: str) -> str:
    try:
        return datetime.strptime(time_str.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(
            f"El horario '{time_str}' no tiene el formato correcto. "
            "Usá HH:MM (por ejemplo, 09:00)."
        )


def build_padel_tools(padel: PadelCalendarClient, config: ClientConfig) -> list[dict]:
    """Returns tool dicts for padel courts, excluding shared tools (get_hours, get_current_date_hour)."""
    tools = [
        _make_get_availability(padel, config),
        _make_create_booking(padel, config),
        _make_cancel_booking(padel, config),
        _make_get_price(config),
    ]
    return tools


def build_padel_payment_tool(
    padel: PadelCalendarClient,
    config: ClientConfig,
    redis: Any,
    user_phone: str,
    client_id: str,
) -> dict:
    cfg = config.tool_config.payment
    if cfg is None:
        raise ValueError("generate_padel_payment_link requires tool_config.payment")
    return _make_generate_padel_payment_link(padel, cfg, redis, user_phone, client_id)


def _make_get_availability(padel: PadelCalendarClient, config: ClientConfig) -> dict:
    async def handler(fecha: str, hora: str, cancha: str | None = None) -> str:
        with logfire.span("tool.get_availability", client_id=str(config.id)):
            try:
                d = _parse_date(fecha)
            except ValueError as exc:
                return str(exc)
            try:
                time_norm = _normalise_time(hora)
            except ValueError as exc:
                return str(exc)

            today = datetime.now(ART).date()
            if d < today:
                return "Esa fecha ya pasó. Elegí un día a partir de hoy."

            try:
                result = padel.get_availability(d, time_norm, cancha)
            except PadelCalendarError as exc:
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
                    "fecha":  {"type": "string", "description": "Fecha en formato AAAA-MM-DD (ej: 2025-03-15)"},
                    "hora":   {"type": "string", "description": "Horario en formato HH:MM (ej: '19:00')"},
                    "cancha": {"type": "string", "description": "Nombre de la cancha (opcional): 'Cancha 1', 'Cancha 2' o 'Cancha 3'"},
                },
                "required": ["fecha", "hora"],
            },
        },
        "handler": handler,
    }


def _make_create_booking(padel: PadelCalendarClient, config: ClientConfig) -> dict:
    async def handler(fecha: str, hora: str, cancha: str, nombre: str, telefono: str) -> str:
        with logfire.span("tool.create_booking", client_id=str(config.id)):
            try:
                d = _parse_date(fecha)
            except ValueError as exc:
                return str(exc)
            try:
                time_norm = _normalise_time(hora)
            except ValueError as exc:
                return str(exc)

            today = datetime.now(ART).date()
            if d < today:
                return "Esa fecha ya pasó. Elegí un día a partir de hoy."

            try:
                booking_id = padel.create_booking(
                    d=d,
                    time_str=time_norm,
                    cancha=cancha.strip(),
                    name=nombre.strip(),
                    phone=telefono.strip(),
                )
            except PadelCalendarError as exc:
                try:
                    avail = padel.get_availability(d, time_norm)
                    free: list[str] = avail.get("available", [])
                except PadelCalendarError:
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
                    "fecha":    {"type": "string", "description": "Fecha en formato AAAA-MM-DD"},
                    "hora":     {"type": "string", "description": "Horario en formato HH:MM (ej: '19:00')"},
                    "cancha":   {"type": "string", "description": "'Cancha 1', 'Cancha 2' o 'Cancha 3'"},
                    "nombre":   {"type": "string", "description": "Nombre completo del cliente"},
                    "telefono": {"type": "string", "description": "Teléfono de contacto"},
                },
                "required": ["fecha", "hora", "cancha", "nombre", "telefono"],
            },
        },
        "handler": handler,
    }


def _make_cancel_booking(padel: PadelCalendarClient, config: ClientConfig) -> dict:
    async def handler(booking_id: str) -> str:
        with logfire.span("tool.cancel_booking", client_id=str(config.id)):
            try:
                found = padel.cancel_booking(booking_id.strip())
            except PadelCalendarError as exc:
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
                    "booking_id": {"type": "string", "description": "Número de reserva recibido al confirmar el turno (ej: 'a3f2c1b0')"},
                },
                "required": ["booking_id"],
            },
        },
        "handler": handler,
    }


def _make_get_price(config: ClientConfig) -> dict:
    async def handler(product: str) -> str:
        with logfire.span("tool.get_price", client_id=str(config.id)):
            query = product.strip().lower()
            for line in config.system_prompt.splitlines():
                line_lower = line.strip().lower()
                if "precio" in line_lower and query in line_lower:
                    return line.strip()
            return (
                f"No encontré el precio para '{product}'. "
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
                    "product": {
                        "type": "string",
                        "description": "Tipo de turno: 'diurno', 'nocturno' o 'fin de semana'",
                    },
                },
                "required": ["product"],
            },
        },
        "handler": handler,
    }


def _make_generate_padel_payment_link(
    padel: PadelCalendarClient,
    payment_cfg: PaymentConfig,
    redis: Any,
    user_phone: str,
    client_id: str,
) -> dict:
    import json as _json
    from app.integrations.mercadopago import MercadoPagoClient, MercadoPagoError

    MP_PAYMENT_TTL = 86400
    mp = MercadoPagoClient(access_token=payment_cfg.access_token, sandbox=payment_cfg.sandbox)

    async def handler(booking_id: str, monto: float) -> str:
        with logfire.span("tool.generate_padel_payment_link", client_id=client_id):
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
            except MercadoPagoError as e:
                logfire.error("tool.generate_padel_payment_link.mp_error", client_id=client_id, error=str(e))
                return "No pude generar el link de pago. Intentá más tarde o coordiná el pago en el complejo."

            redis.set(
                f"mp_payment:{preference.preference_id}",
                _json.dumps({
                    "user_phone": user_phone,
                    "client_id": client_id,
                    "product": title,
                    "quantity": 1,
                    "unit_price": float(monto),
                    "total": float(monto),
                    "booking_id": booking_id.strip(),
                }),
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
                    "booking_id": {"type": "string", "description": "Número de reserva obtenido con create_booking"},
                    "monto":      {"type": "number", "description": "Monto a cobrar en ARS"},
                },
                "required": ["booking_id", "monto"],
            },
        },
        "handler": handler,
    }
