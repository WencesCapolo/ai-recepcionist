"""
app/integrations/google_calendar.py

Real Google Calendar client for the AI receptionist.

Exposes:
  GoogleCalendarClient.available_slots(d)  -> {"morning": [...], "afternoon": [...]}
  GoogleCalendarClient.book(...)           -> event_id str | raises CalendarSlotError
  GoogleCalendarClient.cancel(event_id)    -> bool
  GoogleCalendarClient.find_by_name(name)  -> list[dict]   (upcoming events)

Slot locking (race-condition guard) is handled via Upstash Redis SET NX,
using the same upstash_redis.Redis client the rest of the app uses (sync).

Env vars consumed (validated at startup by config.py):
  GOOGLE_SERVICE_ACCOUNT_JSON — full JSON string of the service account key
  (calendar_id is per-client, stored in Supabase ClientConfig)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Argentine Standard Time — fixed offset, no DST
_ART = timezone(timedelta(hours=-3))

SLOT_DURATION_MIN = 30

MORNING_START = (9, 0)
MORNING_END   = (13, 0)
AFTERNOON_START = (15, 0)
AFTERNOON_END   = (18, 0)

WORK_DAYS = {0, 1, 2, 3, 4}  # Mon–Fri

# Slot lock TTL: 5 minutes — long enough for the user to confirm, short enough
# to not block the slot indefinitely if the conversation is abandoned.
SLOT_LOCK_TTL = 300

# How far ahead to look when listing upcoming appointments
APPOINTMENT_LOOKAHEAD_DAYS = 60


# ---------------------------------------------------------------------------
# Lazy singleton for the Google API service object
# (one per process — credentials are immutable, the object is thread-safe)
# ---------------------------------------------------------------------------

_service: Any = None


def _get_service() -> Any:
    global _service
    if _service is None:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        raw = settings.google_service_account_json
        if not raw:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not configured. "
                "Add it to your environment variables."
            )
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info("Google Calendar service initialised")
    return _service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_slots_for_day() -> list[str]:
    """Return all HH:MM slot strings in working hours."""
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


_ALL_SLOTS: list[str] = _all_slots_for_day()

_MONTHS_ES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_DAYS_ES = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
]


def _friendly_date(d: date) -> str:
    return f"{_DAYS_ES[d.weekday()]} {d.day} de {_MONTHS_ES[d.month]} de {d.year}"


def _slot_datetime(d: date, time_str: str) -> datetime:
    """Combine a date and 'HH:MM' into an ART-aware datetime."""
    h, m = map(int, time_str.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=_ART)


def _busy_intervals(freebusy_response: dict, calendar_id: str) -> list[tuple[datetime, datetime]]:
    busy = freebusy_response.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    result = []
    for b in busy:
        start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
        end   = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        result.append((start, end))
    return result


def _slot_overlaps(slot_start: datetime, busy: list[tuple[datetime, datetime]]) -> bool:
    slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MIN)
    for b_start, b_end in busy:
        if slot_start < b_end and slot_end > b_start:
            return True
    return False


# ---------------------------------------------------------------------------
# GoogleCalendarClient
# ---------------------------------------------------------------------------

class GoogleCalendarClient:
    """
    Google Calendar-backed appointment client.

    Args:
        calendar_id: The Google Calendar ID (e.g. "foo@gmail.com" or the
                     opaque calendar ID from the Calendar settings page).
        redis:       upstash_redis.Redis instance (sync, already validated).
        client_id:   Tenant identifier — used to namespace Redis lock keys.
    """

    def __init__(self, calendar_id: str, redis: Any, client_id: str) -> None:
        self._cal_id = calendar_id
        self._redis  = redis
        self._client_id = client_id

    # ── Redis slot locking ──────────────────────────────────────────────────

    def _lock_key(self, date_iso: str, time_str: str) -> str:
        return f"gcal_lock:{self._client_id}:{date_iso}:{time_str}"

    def _try_lock_slot(self, date_iso: str, time_str: str, conversation_id: str) -> bool:
        """SET NX — returns True if lock acquired, False if already held."""
        key = self._lock_key(date_iso, time_str)
        result = self._redis.set(key, conversation_id, nx=True, ex=SLOT_LOCK_TTL)
        return result is not None  # upstash returns the string "OK" or None

    def _release_slot(self, date_iso: str, time_str: str) -> None:
        self._redis.delete(self._lock_key(date_iso, time_str))

    def _slot_is_locked(self, date_iso: str, time_str: str) -> bool:
        return self._redis.exists(self._lock_key(date_iso, time_str)) == 1

    # ── Google Calendar queries ─────────────────────────────────────────────

    def _freebusy(self, time_min: datetime, time_max: datetime) -> list[tuple[datetime, datetime]]:
        svc = _get_service()
        body = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "items":   [{"id": self._cal_id}],
        }
        resp = svc.freebusy().query(body=body).execute()
        return _busy_intervals(resp, self._cal_id)

    def _is_gcal_busy(self, slot_dt: datetime) -> bool:
        slot_end = slot_dt + timedelta(minutes=SLOT_DURATION_MIN)
        busy = self._freebusy(slot_dt, slot_end)
        return bool(busy)

    # ── Public API (mirrors CalendarMock interface) ─────────────────────────

    def available_slots(self, d: date) -> dict[str, list[str]]:
        """
        Return free slots for *d* grouped into morning/afternoon.
        Checks Google Calendar freebusy + Redis locks.
        Weekend → empty lists.
        """
        if d.weekday() >= 5:
            return {"morning": [], "afternoon": []}

        date_iso = d.isoformat()

        # Build datetimes for all candidate slots
        slot_datetimes = {t: _slot_datetime(d, t) for t in _ALL_SLOTS}

        # Single freebusy call covering the whole day
        day_start = _slot_datetime(d, _ALL_SLOTS[0])
        day_end   = _slot_datetime(d, _ALL_SLOTS[-1]) + timedelta(minutes=SLOT_DURATION_MIN)
        try:
            busy = self._freebusy(day_start, day_end)
        except Exception:
            logger.exception("freebusy query failed for %s", date_iso)
            busy = []

        morning_cutoff = datetime(2000, 1, 1, *AFTERNOON_START)
        morning: list[str] = []
        afternoon: list[str] = []

        for time_str, slot_dt in slot_datetimes.items():
            if _slot_overlaps(slot_dt, busy):
                continue
            if self._slot_is_locked(date_iso, time_str):
                continue
            ref = datetime.strptime(time_str, "%H:%M")
            if ref < morning_cutoff:
                morning.append(time_str)
            else:
                afternoon.append(time_str)

        return {"morning": morning, "afternoon": afternoon}

    def book(
        self,
        d: date,
        time: str,
        name: str,
        phone: str,
        reason: str,
        conversation_id: str = "",
        is_new_patient: bool = True,
    ) -> str:
        """
        Book the slot. Returns the Google Calendar event_id on success.
        Raises CalendarSlotError on invalid input or if the slot is taken.
        """
        if d.weekday() >= 5:
            raise CalendarSlotError("No hay turnos disponibles los fines de semana.")
        if time not in _ALL_SLOTS:
            raise CalendarSlotError(
                f"El horario '{time}' no es válido. Los turnos son cada 30 minutos."
            )

        date_iso = d.isoformat()

        # 1. Acquire Redis lock (fast path — prevents double-booking between
        #    concurrent conversations without hitting the Calendar API twice)
        if not self._try_lock_slot(date_iso, time, conversation_id or name):
            raise CalendarSlotError(
                "Ese horario acaba de ser reservado por otro paciente. "
                "Elegí otro de los disponibles."
            )

        try:
            # 2. Double-check against Google Calendar (race-condition guard)
            slot_dt = _slot_datetime(d, time)
            if self._is_gcal_busy(slot_dt):
                raise CalendarSlotError(
                    "Ese horario ya no está disponible. Elegí otro de los disponibles."
                )

            # 3. Create the event
            slot_end = slot_dt + timedelta(minutes=SLOT_DURATION_MIN)
            patient_type = "Paciente nuevo" if is_new_patient else "Paciente existente"
            event_body = {
                "summary": f"{name} — {reason}",
                "description": (
                    f"Paciente: {name}\n"
                    f"WhatsApp: {phone}\n"
                    f"Motivo: {reason}\n"
                    f"Tipo: {patient_type}\n"
                    f"Agendado via WhatsApp Bot"
                ),
                "start": {
                    "dateTime": slot_dt.isoformat(),
                    "timeZone": "America/Argentina/Cordoba",
                },
                "end": {
                    "dateTime": slot_end.isoformat(),
                    "timeZone": "America/Argentina/Cordoba",
                },
                "reminders": {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": 60}],
                },
            }
            svc = _get_service()
            created = svc.events().insert(calendarId=self._cal_id, body=event_body).execute()
            event_id: str = created["id"]

            logger.info(
                "Appointment booked [client=%s date=%s time=%s name=%s event_id=%s]",
                self._client_id, date_iso, time, name, event_id,
            )
            return event_id

        except CalendarSlotError:
            # Release lock so the slot stays available for others
            self._release_slot(date_iso, time)
            raise
        except Exception as exc:
            self._release_slot(date_iso, time)
            logger.exception("Error creating calendar event [client=%s]", self._client_id)
            raise CalendarSlotError(
                "No se pudo confirmar el turno por un error interno. Intentá de nuevo."
            ) from exc

    def find_by_name(self, patient_name: str) -> list[dict]:
        """
        Search upcoming events matching *patient_name*.
        Returns a list of dicts with keys: event_id, summary, start_dt.
        """
        svc = _get_service()
        now = datetime.now(tz=timezone.utc)
        time_max = now + timedelta(days=APPOINTMENT_LOOKAHEAD_DAYS)

        result = (
            svc.events()
            .list(
                calendarId=self._cal_id,
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                q=patient_name,
                singleEvents=True,
                orderBy="startTime",
                maxResults=5,
            )
            .execute()
        )
        items = result.get("items", [])
        appointments = []
        for ev in items:
            raw = ev["start"].get("dateTime", ev["start"].get("date"))
            start_dt = datetime.fromisoformat(raw)
            appointments.append({
                "event_id": ev["id"],
                "summary":  ev.get("summary", "(sin título)"),
                "start_dt": start_dt,
            })
        return appointments

    def cancel(self, event_id: str, patient_name: str) -> str:
        """
        Cancel an event by ID. Verifies patient name matches before deleting.
        Returns a human-readable confirmation string.
        Raises CalendarSlotError if the event is not found or name doesn't match.
        """
        svc = _get_service()
        try:
            event = svc.events().get(calendarId=self._cal_id, eventId=event_id).execute()
        except Exception:
            raise CalendarSlotError(
                "No encontré ese turno. Verificá el ID o buscá el turno de nuevo."
            )

        summary = event.get("summary", "")
        # Check only the first word of the name to handle partial matches
        first_name = patient_name.strip().lower().split()[0]
        if first_name not in summary.lower():
            raise CalendarSlotError(
                f"El turno no parece corresponder a {patient_name}. Verificá los datos."
            )

        raw = event["start"].get("dateTime", event["start"].get("date"))
        start_dt = datetime.fromisoformat(raw).astimezone(_ART)
        svc.events().delete(calendarId=self._cal_id, eventId=event_id).execute()

        d = start_dt.date()
        time_str = start_dt.strftime("%H:%M")
        date_iso = d.isoformat()
        # Release any lingering lock for the freed slot
        self._release_slot(date_iso, time_str)

        logger.info(
            "Appointment cancelled [client=%s event_id=%s name=%s]",
            self._client_id, event_id, patient_name,
        )
        return f"Turno cancelado: {_friendly_date(d)} a las {time_str} para {patient_name}."

    def reschedule(
        self,
        event_id: str,
        new_date: date,
        new_time: str,
        conversation_id: str = "",
    ) -> str:
        """
        Move an existing event to a new slot.
        Returns a confirmation string.
        Raises CalendarSlotError on conflicts or infrastructure errors.
        """
        if new_date.weekday() >= 5:
            raise CalendarSlotError("No trabajamos los fines de semana.")
        if new_time not in _ALL_SLOTS:
            raise CalendarSlotError(
                f"El horario '{new_time}' no es válido. Los turnos son cada 30 minutos."
            )

        date_iso = new_date.isoformat()

        if not self._try_lock_slot(date_iso, new_time, conversation_id):
            raise CalendarSlotError(
                "Ese horario acaba de ser reservado. Elegí otro de los disponibles."
            )

        try:
            new_start = _slot_datetime(new_date, new_time)
            if self._is_gcal_busy(new_start):
                raise CalendarSlotError(
                    "Ese horario ya no está disponible. Elegí otro."
                )

            new_end = new_start + timedelta(minutes=SLOT_DURATION_MIN)
            svc = _get_service()
            event = svc.events().get(calendarId=self._cal_id, eventId=event_id).execute()
            event["start"] = {
                "dateTime": new_start.isoformat(),
                "timeZone": "America/Argentina/Cordoba",
            }
            event["end"] = {
                "dateTime": new_end.isoformat(),
                "timeZone": "America/Argentina/Cordoba",
            }
            svc.events().update(
                calendarId=self._cal_id, eventId=event_id, body=event
            ).execute()

            logger.info(
                "Appointment rescheduled [client=%s event_id=%s new_date=%s new_time=%s]",
                self._client_id, event_id, date_iso, new_time,
            )
            return (
                f"Turno reprogramado al {_friendly_date(new_date)} a las {new_time}. "
                f"ID: {event_id}"
            )

        except CalendarSlotError:
            self._release_slot(date_iso, new_time)
            raise
        except Exception as exc:
            self._release_slot(date_iso, new_time)
            logger.exception("Error rescheduling event [client=%s]", self._client_id)
            raise CalendarSlotError(
                "No se pudo reprogramar el turno por un error interno. Intentá de nuevo."
            ) from exc


class CalendarSlotError(Exception):
    """Raised for invalid slot inputs, conflicts, or infrastructure errors."""
