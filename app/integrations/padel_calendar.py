"""
padel_calendar.py — Redis-backed calendar for padel court bookings.

Three courts: "Cancha 1" (techada), "Cancha 2" (techada), "Cancha 3" (aire libre).
Slots: Mon–Fri 08:00–23:00, Sat–Sun 08:00–22:00, every 60 min.

Pre-seeded taken slots per weekday keep the demo looking realistic.
Real bookings are written to Redis with a 30-day TTL.
Cancellations use a booking_id returned at creation time.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

COURTS: list[str] = ["Cancha 1", "Cancha 2", "Cancha 3"]
SLOT_DURATION_MIN = 60
BOOKING_TTL = 30 * 24 * 3600  # 30 days

# Pre-seeded "already taken" slots per weekday: {weekday: [(time, cancha), ...]}
_PRESEED: dict[int, list[tuple[str, str]]] = {
    0: [("09:00", "Cancha 1"), ("11:00", "Cancha 2"), ("20:00", "Cancha 3")],
    1: [("10:00", "Cancha 1"), ("10:00", "Cancha 2"), ("19:00", "Cancha 1")],
    2: [("09:00", "Cancha 3"), ("14:00", "Cancha 1"), ("21:00", "Cancha 2")],
    3: [("11:00", "Cancha 2"), ("15:00", "Cancha 3"), ("20:00", "Cancha 1")],
    4: [("09:00", "Cancha 1"), ("13:00", "Cancha 3"), ("19:00", "Cancha 2")],
    5: [("10:00", "Cancha 1"), ("12:00", "Cancha 2"), ("16:00", "Cancha 3")],
    6: [("09:00", "Cancha 2"), ("11:00", "Cancha 1"), ("15:00", "Cancha 3")],
}


def _build_slots(start_h: int, end_h: int) -> list[str]:
    slots: list[str] = []
    current = datetime(2000, 1, 1, start_h, 0)
    stop = datetime(2000, 1, 1, end_h, 0)
    while current < stop:
        slots.append(current.strftime("%H:%M"))
        current += timedelta(minutes=SLOT_DURATION_MIN)
    return slots


WEEKDAY_SLOTS: list[str] = _build_slots(8, 23)  # 08:00–22:00 (last starts 22:00)
WEEKEND_SLOTS: list[str] = _build_slots(8, 22)  # 08:00–21:00 (last starts 21:00)


class PadelCalendar:
    """
    Padel court calendar backed by Redis.

    Redis key patterns:
      padel:slot:{client_id}:{date_iso}:{time}:{cancha_norm}
          value: booking_id string — TTL: BOOKING_TTL

      padel:booking:{client_id}:{booking_id}
          value: JSON {date, time, cancha, name, phone, booked_at} — TTL: BOOKING_TTL
    """

    def __init__(self, redis: Any, client_id: str) -> None:
        self._redis = redis
        self._client_id = client_id

    # ── internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _cancha_norm(cancha: str) -> str:
        """'Cancha 1' → 'cancha1' for use in Redis keys."""
        return "".join(c for c in cancha.lower() if c.isalnum())

    @staticmethod
    def resolve_cancha(cancha: str) -> str:
        """Case-insensitive match against COURTS. Raises PadelSlotError if not found."""
        normalised = cancha.strip().lower()
        for court in COURTS:
            if court.lower() == normalised:
                return court
        raise PadelSlotError(
            f"La cancha '{cancha}' no existe. Las opciones son: "
            + ", ".join(COURTS) + "."
        )

    def _slot_key(self, date_iso: str, time: str, cancha: str) -> str:
        return f"padel:slot:{self._client_id}:{date_iso}:{time}:{self._cancha_norm(cancha)}"

    def _booking_key(self, booking_id: str) -> str:
        return f"padel:booking:{self._client_id}:{booking_id}"

    def valid_slots(self, d: date) -> list[str]:
        return WEEKEND_SLOTS if d.weekday() >= 5 else WEEKDAY_SLOTS

    def _is_preseed_taken(self, d: date, time: str, cancha: str) -> bool:
        for t, c in _PRESEED.get(d.weekday(), []):
            if t == time and c == cancha:
                return True
        return False

    def _is_redis_taken(self, date_iso: str, time: str, cancha: str) -> bool:
        key = self._slot_key(date_iso, time, cancha)
        try:
            return self._redis.exists(key) == 1
        except Exception:
            logger.warning("Redis error checking padel slot %s %s %s", date_iso, time, cancha)
            return False

    def _is_taken(self, d: date, time: str, cancha: str) -> bool:
        return self._is_preseed_taken(d, time, cancha) or self._is_redis_taken(
            d.isoformat(), time, cancha
        )

    # ── public API ───────────────────────────────────────────────────────────

    def get_availability(
        self, d: date, time: str, cancha: str | None = None
    ) -> dict:
        """
        Check availability at a specific time.

        Returns:
          No cancha: {"time": "19:00", "available": ["Cancha 1", "Cancha 3"]}
          With cancha: {"time": "19:00", "cancha": "Cancha 1", "available": True/False}
        """
        slots = self.valid_slots(d)
        if time not in slots:
            raise PadelSlotError(
                f"El horario '{time}' no es válido para ese día. "
                f"Los turnos son cada hora de {slots[0]} a {slots[-1]}."
            )

        if cancha is not None:
            resolved = self.resolve_cancha(cancha)
            return {
                "time": time,
                "cancha": resolved,
                "available": not self._is_taken(d, time, resolved),
            }

        free = [c for c in COURTS if not self._is_taken(d, time, c)]
        return {"time": time, "available": free}

    def create_booking(
        self,
        d: date,
        time: str,
        cancha: str,
        name: str,
        phone: str,
    ) -> str:
        """
        Reserve a court slot. Returns the booking_id on success.
        Raises PadelSlotError if the slot is taken, invalid, or on infra error.
        """
        slots = self.valid_slots(d)
        if time not in slots:
            raise PadelSlotError(
                f"El horario '{time}' no es válido para ese día. "
                f"Los turnos son cada hora de {slots[0]} a {slots[-1]}."
            )
        resolved = self.resolve_cancha(cancha)

        if self._is_taken(d, time, resolved):
            raise PadelSlotError(f"La {resolved} ya está ocupada a las {time}.")

        booking_id = str(uuid.uuid4())[:8]
        date_iso = d.isoformat()
        slot_key = self._slot_key(date_iso, time, resolved)
        booking_meta = json.dumps({
            "date": date_iso,
            "time": time,
            "cancha": resolved,
            "name": name,
            "phone": phone,
            "booked_at": datetime.utcnow().isoformat(),
        })

        try:
            result = self._redis.set(slot_key, booking_id, ex=BOOKING_TTL, nx=True)
            if result is None:
                raise PadelSlotError(f"La {resolved} ya está ocupada a las {time}.")
            self._redis.set(self._booking_key(booking_id), booking_meta, ex=BOOKING_TTL)
        except PadelSlotError:
            raise
        except Exception as exc:
            logger.error(
                "Redis error creating padel booking %s %s %s: %s",
                date_iso, time, resolved, exc,
            )
            raise PadelSlotError(
                "No se pudo confirmar la reserva por un error interno. Intentá de nuevo."
            ) from exc

        logger.info(
            "Padel booking created [client=%s id=%s date=%s time=%s cancha=%s name=%s]",
            self._client_id, booking_id, date_iso, time, resolved, name,
        )
        return booking_id

    def cancel_booking(self, booking_id: str) -> bool:
        """
        Cancel by booking_id. Returns True if found and deleted, False if not found.
        Raises PadelSlotError on infrastructure errors.
        """
        booking_key = self._booking_key(booking_id)
        try:
            raw = self._redis.get(booking_key)
        except Exception as exc:
            logger.error("Redis error fetching padel booking %s: %s", booking_id, exc)
            raise PadelSlotError(
                "No se pudo cancelar la reserva por un error interno. Intentá de nuevo."
            ) from exc

        if raw is None:
            return False

        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.error("Corrupt padel booking metadata for %s", booking_id)
            return False

        slot_key = self._slot_key(meta["date"], meta["time"], meta["cancha"])
        try:
            self._redis.delete(slot_key)
            self._redis.delete(booking_key)
        except Exception as exc:
            logger.error("Redis error deleting padel booking %s: %s", booking_id, exc)
            raise PadelSlotError(
                "No se pudo cancelar la reserva por un error interno. Intentá de nuevo."
            ) from exc

        logger.info(
            "Padel booking cancelled [client=%s id=%s date=%s time=%s cancha=%s]",
            self._client_id, booking_id, meta["date"], meta["time"], meta["cancha"],
        )
        return True


class PadelSlotError(Exception):
    """Raised for invalid inputs or infrastructure errors."""
