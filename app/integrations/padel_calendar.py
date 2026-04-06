"""
padel_calendar.py — Google Calendar backend for padel court bookings.

Three courts share a single Google Calendar.
Court name is embedded in the event summary to allow per-court availability checks.
Redis is used only for slot locking (same pattern as calendar.py).
Booking metadata is stored in Redis so the payment link tool can look it up.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.integrations.calendar import _get_service
from app.integrations.argentina import ART
from app.integrations.calendar_config import SLOT_LOCK_TTL as LOCK_TTL, APPOINTMENT_TTL as BOOKING_TTL

logger = logging.getLogger(__name__)

COURTS: list[str] = ["Cancha 1", "Cancha 2", "Cancha 3"]
SLOT_MINUTES = 60


def resolve_cancha(cancha: str) -> str:
    """Case-insensitive match against COURTS. Raises PadelCalendarError if not found."""
    normalised = cancha.strip().lower()
    for court in COURTS:
        if court.lower() == normalised:
            return court
    raise PadelCalendarError(
        f"La cancha '{cancha}' no existe. Las opciones son: " + ", ".join(COURTS) + "."
    )


class PadelCalendarClient:
    """
    Padel court calendar backed by Google Calendar.

    All three courts share one calendar_id.
    Events are titled "{cancha} — {name}" so availability checks can
    filter by court name.

    Redis key patterns (mirrors calendar.py):
      slot_lock:{client_id}:{date_iso}:{time}:{cancha_norm}  — LOCK_TTL
      padel:booking:{client_id}:{event_id}                   — BOOKING_TTL
    """

    def __init__(self, calendar_id: str, redis: Any, client_id: str) -> None:
        self.calendar_id = calendar_id.strip()
        self._redis = redis
        self._client_id = client_id
        self._service = None

    @property
    def service(self):
        if self._service is None:
            self._service = _get_service()
        return self._service

    # ── Redis slot locking ────────────────────────────────────────────────────

    def _lock_key(self, date_iso: str, time_str: str, cancha: str) -> str:
        norm = "".join(c for c in cancha.lower() if c.isalnum())
        return f"slot_lock:{self._client_id}:{date_iso}:{time_str}:{norm}"

    def _acquire_lock(self, date_iso: str, time_str: str, cancha: str, owner: str) -> bool:
        key = self._lock_key(date_iso, time_str, cancha)
        return bool(self._redis.set(key, owner, nx=True, ex=LOCK_TTL))

    def _slot_locked(self, date_iso: str, time_str: str, cancha: str) -> bool:
        return bool(self._redis.exists(self._lock_key(date_iso, time_str, cancha)))

    def _release_lock(self, date_iso: str, time_str: str, cancha: str) -> None:
        self._redis.delete(self._lock_key(date_iso, time_str, cancha))

    # ── Booking metadata (for payment link lookups) ───────────────────────────

    def _store_booking_meta(
        self,
        event_id: str,
        date_iso: str,
        time_str: str,
        cancha: str,
        name: str,
        phone: str,
    ) -> None:
        key = f"padel:booking:{self._client_id}:{event_id}"
        self._redis.set(
            key,
            json.dumps({"date": date_iso, "time": time_str, "cancha": cancha, "name": name, "phone": phone}),
            ex=BOOKING_TTL,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_availability(
        self, d: date, time_str: str, cancha: str | None = None
    ) -> dict:
        """
        Check which courts are free at a specific date and time.

        No cancha → {"time": "19:00", "available": ["Cancha 1", "Cancha 3"]}
        With cancha → {"time": "19:00", "cancha": "Cancha 1", "available": True/False}
        """
        h, m = int(time_str.split(":")[0]), int(time_str.split(":")[1])
        slot_start = datetime(d.year, d.month, d.day, h, m, tzinfo=ART)
        slot_end = slot_start + timedelta(minutes=SLOT_MINUTES)

        try:
            items = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=slot_start.isoformat(),
                timeMax=slot_end.isoformat(),
                singleEvents=True,
            ).execute().get("items", [])
        except Exception as exc:
            logger.error("get_availability error: %s", exc)
            raise PadelCalendarError(
                "No pude verificar la disponibilidad. Intentá más tarde."
            ) from exc

        busy_courts: set[str] = set()
        for event in items:
            summary = event.get("summary", "")
            for court in COURTS:
                if court.lower() in summary.lower():
                    busy_courts.add(court)

        date_iso = d.isoformat()

        if cancha is not None:
            resolved = resolve_cancha(cancha)
            locked = self._slot_locked(date_iso, time_str, resolved)
            return {
                "time": time_str,
                "cancha": resolved,
                "available": resolved not in busy_courts and not locked,
            }

        free = [
            c for c in COURTS
            if c not in busy_courts and not self._slot_locked(date_iso, time_str, c)
        ]
        return {"time": time_str, "available": free}

    def create_booking(
        self,
        d: date,
        time_str: str,
        cancha: str,
        name: str,
        phone: str,
    ) -> str:
        """
        Create a Google Calendar event. Returns the event ID as booking_id.
        Raises PadelCalendarError on conflict or infra error.
        """
        resolved = resolve_cancha(cancha)
        date_iso = d.isoformat()
        owner = f"{phone}:{date_iso}:{time_str}:{resolved}"

        if not self._acquire_lock(date_iso, time_str, resolved, owner):
            raise PadelCalendarError(
                f"La {resolved} está siendo reservada en este momento. Intentá en unos segundos."
            )

        try:
            avail = self.get_availability(d, time_str, resolved)
            if not avail["available"]:
                raise PadelCalendarError(f"La {resolved} ya está ocupada a las {time_str}.")

            h, m = int(time_str.split(":")[0]), int(time_str.split(":")[1])
            slot_start = datetime(d.year, d.month, d.day, h, m, tzinfo=ART)
            slot_end = slot_start + timedelta(minutes=SLOT_MINUTES)

            event = {
                "summary": f"{resolved} — {name}",
                "description": (
                    f"Cliente: {name}\n"
                    f"Teléfono: {phone}\n"
                    f"Cancha: {resolved}\n"
                    f"Agendado via WhatsApp Bot"
                ),
                "start": {
                    "dateTime": slot_start.isoformat(),
                    "timeZone": "America/Argentina/Cordoba",
                },
                "end": {
                    "dateTime": slot_end.isoformat(),
                    "timeZone": "America/Argentina/Cordoba",
                },
            }
            created = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event,
                sendUpdates="none",
            ).execute()
            event_id = created["id"]

            self._store_booking_meta(event_id, date_iso, time_str, resolved, name, phone)

            logger.info(
                "Padel booking created [client=%s court=%s date=%s time=%s name=%s event_id=%s]",
                self._client_id, resolved, date_iso, time_str, name, event_id,
            )
            return event_id

        except PadelCalendarError:
            self._release_lock(date_iso, time_str, resolved)
            raise
        except Exception as exc:
            logger.error("create_booking error: %s", exc)
            self._release_lock(date_iso, time_str, resolved)
            raise PadelCalendarError(
                "No pude confirmar la reserva. Intentá más tarde."
            ) from exc

    def cancel_booking(self, booking_id: str) -> bool:
        """
        Cancel by Google Calendar event ID.
        Returns True if deleted, False if not found.
        Raises PadelCalendarError on infra error.
        """
        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=booking_id.strip(),
                sendUpdates="none",
            ).execute()
        except Exception as exc:
            err_str = str(exc)
            if "404" in err_str or "notFound" in err_str.lower():
                return False
            logger.error("cancel_booking error: %s", exc)
            raise PadelCalendarError(
                "No pude cancelar la reserva. Intentá más tarde o contactá al complejo."
            ) from exc

        logger.info(
            "Padel booking cancelled [client=%s event_id=%s]",
            self._client_id, booking_id,
        )
        return True


class PadelCalendarError(Exception):
    """Raised for booking conflicts, invalid inputs, or infrastructure errors."""
