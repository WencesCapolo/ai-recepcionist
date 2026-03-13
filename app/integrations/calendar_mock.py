"""
calendar_mock.py — deterministic fake availability for the dentist demo.

Slots: Mon–Fri, 09:00–13:00 and 15:00–18:00, every 30 min.
Pre-seeded taken slots are keyed by weekday (0=Mon … 4=Fri) so the
demo always looks the same on any given day of the week.
Real bookings made during the demo are written to Redis and block
those slots for 7 days (604 800 s).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

SLOT_DURATION_MIN = 30
MORNING_START = (9, 0)
MORNING_END = (13, 0)
AFTERNOON_START = (15, 0)
AFTERNOON_END = (18, 0)
BOOKING_TTL = 7 * 24 * 3600  # 7 days

# Pre-seeded "already taken" slots per weekday (0=Mon … 4=Fri).
# Times are "HH:MM" strings — must be on the 30-min grid.
_PRESEED: dict[int, list[str]] = {
    0: ["09:00", "10:00", "15:30"],          # Monday
    1: ["09:30", "11:00", "16:00", "16:30"], # Tuesday
    2: ["10:00", "10:30", "15:00"],          # Wednesday
    3: ["09:00", "12:00", "17:00"],          # Thursday
    4: ["09:30", "11:30", "15:30", "17:00"], # Friday
}


def _all_slots() -> list[str]:
    """Return all slots in a day as 'HH:MM' strings."""
    slots: list[str] = []
    for (sh, sm), (eh, em) in [
        (MORNING_START, MORNING_END),
        (AFTERNOON_START, AFTERNOON_END),
    ]:
        current = datetime(2000, 1, 1, sh, sm)
        end = datetime(2000, 1, 1, eh, em)
        while current < end:
            slots.append(current.strftime("%H:%M"))
            current += timedelta(minutes=SLOT_DURATION_MIN)
    return slots


ALL_SLOTS: list[str] = _all_slots()


class CalendarMock:
    """
    Fake calendar backed by Redis.

    Redis key format: ``calendar:{client_id}:{date_iso}:{time}``
    Value: JSON-encoded booking dict (name, phone, reason).
    """

    def __init__(self, redis: Any, client_id: str) -> None:
        self._redis = redis
        self._client_id = client_id

    # ── internal helpers ─────────────────────────────────────────────────────

    def _redis_key(self, date_iso: str, time: str) -> str:
        return f"calendar:{self._client_id}:{date_iso}:{time}"

    def _is_preseed_taken(self, d: date, time: str) -> bool:
        weekday = d.weekday()  # 0=Mon … 6=Sun
        return time in _PRESEED.get(weekday, [])

    def _is_redis_taken(self, date_iso: str, time: str) -> bool:
        key = self._redis_key(date_iso, time)
        try:
            return self._redis.exists(key) == 1
        except Exception:
            logger.warning("Redis error checking slot %s %s", date_iso, time)
            return False

    def _is_taken(self, d: date, time: str) -> bool:
        return self._is_preseed_taken(d, time) or self._is_redis_taken(
            d.isoformat(), time
        )

    # ── public API ───────────────────────────────────────────────────────────

    def available_slots(self, d: date) -> dict[str, list[str]]:
        """
        Return free slots grouped by period.

        Returns ``{"morning": [...], "afternoon": [...]}`` with only free slots.
        Weekend → empty lists.
        """
        if d.weekday() >= 5:  # Sat / Sun
            return {"morning": [], "afternoon": []}

        morning_boundary = datetime(2000, 1, 1, *AFTERNOON_START)

        morning: list[str] = []
        afternoon: list[str] = []

        for slot in ALL_SLOTS:
            if self._is_taken(d, slot):
                continue
            slot_dt = datetime.strptime(slot, "%H:%M")
            if slot_dt < morning_boundary:
                morning.append(slot)
            else:
                afternoon.append(slot)

        return {"morning": morning, "afternoon": afternoon}

    def book(
        self,
        d: date,
        time: str,
        name: str,
        phone: str,
        reason: str,
    ) -> bool:
        """
        Attempt to book *time* on *d*.

        Returns True on success, False if the slot is already taken.
        Raises CalendarSlotError on bad inputs.
        """
        if d.weekday() >= 5:
            raise CalendarSlotError("No hay turnos disponibles los fines de semana.")
        if time not in ALL_SLOTS:
            raise CalendarSlotError(
                f"El horario '{time}' no es válido. Los turnos son cada 30 minutos."
            )
        if self._is_taken(d, time):
            return False  # caller decides the message

        payload = json.dumps(
            {
                "name": name,
                "phone": phone,
                "reason": reason,
                "booked_at": datetime.utcnow().isoformat(),
            }
        )
        key = self._redis_key(d.isoformat(), time)
        try:
            # SET NX — only write if key doesn't exist (race condition guard)
            result = self._redis.set(key, payload, ex=BOOKING_TTL, nx=True)
            if result is None:
                # Another concurrent request beat us to it
                return False
        except Exception as exc:
            logger.error("Redis error booking slot %s %s: %s", d.isoformat(), time, exc)
            raise CalendarSlotError(
                "No se pudo confirmar el turno por un error interno. Intentá de nuevo."
            ) from exc

        logger.info(
            "Slot booked [client=%s date=%s time=%s name=%s]",
            self._client_id,
            d.isoformat(),
            time,
            name,
        )
        return True

    def cancel(self, d: date, time: str) -> bool:
        """
        Cancel a previously booked slot.

        Returns True if the booking was found and deleted.
        Returns False if there was no Redis booking for that slot
        (pre-seeded slots or already cancelled).
        Raises CalendarSlotError on infrastructure errors.
        """
        if time not in ALL_SLOTS:
            raise CalendarSlotError(
                f"El horario '{time}' no es válido. Los turnos son cada 30 minutos."
            )
        key = self._redis_key(d.isoformat(), time)
        try:
            deleted = self._redis.delete(key)
            found = deleted == 1
        except Exception as exc:
            logger.error(
                "Redis error cancelling slot %s %s: %s", d.isoformat(), time, exc
            )
            raise CalendarSlotError(
                "No se pudo cancelar el turno por un error interno. Intentá de nuevo."
            ) from exc

        if found:
            logger.info(
                "Slot cancelled [client=%s date=%s time=%s]",
                self._client_id,
                d.isoformat(),
                time,
            )
        return found


class CalendarSlotError(Exception):
    """Raised for invalid slot inputs or infrastructure errors."""
