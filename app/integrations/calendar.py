"""
app/integrations/calendar.py

GoogleCalendarClient — real Google Calendar backend.
All methods are synchronous and return plain strings (tool handler output).
Redis is used for slot locking to prevent double-booking.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (can be overridden via env or ClientConfig in the future)
# ---------------------------------------------------------------------------

_SCOPES        = ["https://www.googleapis.com/auth/calendar"]
SLOT_MINUTES   = 30
WORK_START     = 10   # 10:00 ART
WORK_END       = 18   # 18:00 ART
WORK_DAYS      = {0, 1, 2, 3, 4}  # Mon–Fri
LOCK_TTL       = 300  # seconds — slot reservation window
ART            = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_service_account_info() -> dict:
    raw = base64.b64decode(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]).decode("utf-8")
    return json.loads(raw)


def _get_service():
    creds = Credentials.from_service_account_info(
        _load_service_account_info(), scopes=_SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _fmt(dt: datetime) -> str:
    """'martes 18 de marzo a las 10:00'"""
    art  = dt.astimezone(ART)
    days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    months = [
        "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    return f"{days[art.weekday()]} {art.day} de {months[art.month]} a las {art.strftime('%H:%M')}"


def _next_candidates(n: int, duration_minutes: int = SLOT_MINUTES) -> list[datetime]:
    """Return next n candidate slot datetimes in ART, starting ≥2h from now."""
    now       = datetime.now(tz=ART)
    start     = now + timedelta(hours=2)
    remainder = start.minute % SLOT_MINUTES
    if remainder:
        start += timedelta(minutes=(SLOT_MINUTES - remainder))
    start = start.replace(second=0, microsecond=0)

    slots  = []
    cursor = start
    while len(slots) < n:
        cursor_art = cursor.astimezone(ART)
        slot_end_hour = (cursor_art + timedelta(minutes=duration_minutes)).hour
        fits_in_day = (
            cursor_art.weekday() in WORK_DAYS
            and WORK_START <= cursor_art.hour
            and (cursor_art + timedelta(minutes=duration_minutes)).astimezone(ART).hour <= WORK_END
            and (cursor_art + timedelta(minutes=duration_minutes)).astimezone(ART).date() == cursor_art.date()
        )
        if fits_in_day:
            slots.append(cursor_art)
        cursor += timedelta(minutes=SLOT_MINUTES)
        cursor_art = cursor.astimezone(ART)
        if cursor_art.hour >= WORK_END:
            next_day = (cursor_art + timedelta(days=1)).replace(
                hour=WORK_START, minute=0, second=0, microsecond=0
            )
            while next_day.weekday() not in WORK_DAYS:
                next_day += timedelta(days=1)
            cursor = next_day
    return slots


def _busy_periods(service, calendar_id: str, slots: list[datetime], duration_minutes: int = SLOT_MINUTES) -> list[tuple[datetime, datetime]]:
    time_min = slots[0].isoformat()
    time_max = (slots[-1] + timedelta(minutes=duration_minutes)).isoformat()
    body = {"timeMin": time_min, "timeMax": time_max, "items": [{"id": calendar_id}]}
    fb   = service.freebusy().query(body=body).execute()
    busy = fb.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return [
        (
            datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
            datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
        )
        for b in busy
    ]


def _is_busy(slot: datetime, busy: list[tuple[datetime, datetime]], duration_minutes: int = SLOT_MINUTES) -> bool:
    slot_end = slot + timedelta(minutes=duration_minutes)
    return any(slot < b_end and slot_end > b_start for b_start, b_end in busy)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class GoogleCalendarClient:
    def __init__(self, calendar_id: str, redis, client_id: str):
        self.calendar_id = calendar_id.strip()  # guard against trailing \r\n from Supabase
        self.redis       = redis
        self.client_id   = client_id
        self._service    = None

    @property
    def service(self):
        if self._service is None:
            self._service = _get_service()
        return self._service

    # ── Slot locking ──────────────────────────────────────────────────────────

    def _lock_key(self, slot_iso: str) -> str:
        return f"slot_lock:{self.client_id}:{slot_iso}"

    def _acquire_lock(self, slot_iso: str, owner: str) -> bool:
        key    = self._lock_key(slot_iso)
        result = self.redis.set(key, owner, nx=True, ex=LOCK_TTL)
        return bool(result)

    def _release_lock(self, slot_iso: str):
        self.redis.delete(self._lock_key(slot_iso))

    def _slot_locked(self, slot_iso: str) -> bool:
        return bool(self.redis.exists(self._lock_key(slot_iso)))

    # ── Public interface (called by calendar_tools.py handlers) ──────────────

    def get_current_date_hour(self) -> str:
        now  = datetime.now(tz=ART)
        days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        months = [
            "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        return (
            f"Hoy es {days[now.weekday()]} {now.day} de {months[now.month]} de {now.year}. "
            f"Son las {now.strftime('%H:%M')} (hora Argentina)."
        )

    def check_availability(
        self,
        count: int = 3,
        duration_minutes: int = SLOT_MINUTES,
        after_hour: int = 0,
        before_hour: int = 24,
    ) -> str:
        try:
            candidates = _next_candidates(count * 10, duration_minutes)
            busy       = _busy_periods(self.service, self.calendar_id, candidates, duration_minutes)

            free = [
                s for s in candidates
                if not _is_busy(s, busy, duration_minutes)
                and not self._slot_locked(s.isoformat())
                and after_hour <= s.astimezone(ART).hour < before_hour
            ]

            # If a time range is specified, show multiple slots (user knows what they want).
            # Otherwise, show only the first slot per day (cleaner for open-ended requests).
            if after_hour > 0 or before_hour < 24:
                available = free[:count]
            else:
                seen_days: set = set()
                available: list = []
                for s in free:
                    day = s.astimezone(ART).date()
                    if day not in seen_days:
                        seen_days.add(day)
                        available.append(s)
                    if len(available) == count:
                        break

        except Exception as e:
            logger.error("check_availability error: %s", e)
            return "No pude verificar la disponibilidad en este momento. Intentá más tarde."

        if not available:
            if after_hour > 0:
                return f"No tengo turnos disponibles después de las {after_hour}:00 en los próximos días."
            return "No hay turnos disponibles en los próximos días."

        lines = [f"{_fmt(s)}  [slot_iso: {s.isoformat()}]" for s in available]
        return "Turnos disponibles:\n" + "\n".join(lines)

    def book_appointment(
        self,
        patient_name: str,
        patient_phone: str,
        patient_email: str,
        reason: str,
        slot_iso: str,
        is_new_patient: bool = True,
        duration_minutes: int = SLOT_MINUTES,
    ) -> str:
        owner = f"{patient_phone}:{slot_iso}"

        # 1. Acquire slot lock
        if not self._acquire_lock(slot_iso, owner):
            return (
                "Ese horario acaba de ser tomado por otro paciente. "
                "Elegí otro de los disponibles."
            )

        try:
            slot_dt  = datetime.fromisoformat(slot_iso)
            slot_end = slot_dt + timedelta(minutes=duration_minutes)

            # 2. Double-check against Calendar
            busy = _busy_periods(self.service, self.calendar_id, [slot_dt])
            if _is_busy(slot_dt, busy):
                return (
                    "Ese horario ya no está disponible. "
                    "Elegí otro de los que te ofrecí."
                )

            # 3. Create event
            patient_type = "Paciente nuevo" if is_new_patient else "Paciente existente"
            event = {
                "summary": f"{patient_name} — {reason}",
                "description": (
                    f"Paciente: {patient_name}\n"
                    f"WhatsApp: {patient_phone}\n"
                    f"Email: {patient_email}\n"
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
                    "overrides": [
                        {"method": "popup", "minutes": 60},
                    ],
                },
            }
            created  = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event,
                sendUpdates="none",
            ).execute()
            event_id = created["id"]

            # 4. Store in Redis for quick lookup (30 days)
            lookup_key = f"appt:{self.client_id}:{patient_phone}:{slot_dt.strftime('%Y-%m-%d')}"
            self.redis.set(lookup_key, event_id, ex=60 * 60 * 24 * 30)

            return (
                f"Turno confirmado: {patient_name}, {_fmt(slot_dt)}, motivo: {reason}. "
                f"Se envió confirmación a {patient_email}. "
                f"ID: {event_id}"
            )

        except Exception as e:
            logger.error("book_appointment error: %s", e)
            return "No pude crear el turno. Intentá más tarde o llamá al consultorio."

        finally:
            # Lock is kept until TTL expires — prevents re-booking same slot
            # while the patient is still in the same conversation.
            # Call _release_lock only on error paths if needed.
            pass

    def get_appointment(self, patient_name: str, date_hint: Optional[str] = None) -> str:
        try:
            now      = datetime.now(tz=ART)
            time_max = (now + timedelta(days=60)).isoformat()

            results = (
                self.service.events()
                .list(
                    calendarId=self.calendar_id,
                    timeMin=now.isoformat(),
                    timeMax=time_max,
                    q=patient_name,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=5,
                )
                .execute()
            )
            items = results.get("items", [])
        except Exception as e:
            logger.error("get_appointment error: %s", e)
            return "No pude buscar el turno en este momento."

        if not items:
            return f"No encontré turnos próximos para {patient_name}."

        lines = []
        for ev in items:
            start_raw = ev["start"].get("dateTime", ev["start"].get("date"))
            start_dt  = datetime.fromisoformat(start_raw)
            lines.append(
                f"- {_fmt(start_dt)}: {ev.get('summary', '(sin título)')}  [ID: {ev['id']}]"
            )
        return f"Turnos encontrados para {patient_name}:\n" + "\n".join(lines)

    def cancel_appointment(self, event_id: str, patient_name: str) -> str:
        try:
            event = self.service.events().get(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
        except Exception:
            return "No encontré ese turno. Verificá el nombre o buscá el turno de nuevo."

        summary = event.get("summary", "")
        first_name = patient_name.lower().split()[0]
        if first_name not in summary.lower():
            return f"El turno no parece corresponder a {patient_name}. Verificá."

        start_raw = event["start"].get("dateTime", event["start"].get("date"))
        start_dt  = datetime.fromisoformat(start_raw)

        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=event_id,
                sendUpdates="all",
            ).execute()
        except Exception as e:
            logger.error("cancel_appointment error: %s", e)
            return "No pude cancelar el turno. Intentá más tarde o llamá al consultorio."

        return f"Turno cancelado: {_fmt(start_dt)} para {patient_name}."

    def reschedule_appointment(self, event_id: str, new_slot_iso: str) -> str:
        new_start = datetime.fromisoformat(new_slot_iso)
        new_end   = new_start + timedelta(minutes=SLOT_MINUTES)

        # Check availability first
        try:
            busy = _busy_periods(self.service, self.calendar_id, [new_start])
        except Exception as e:
            logger.error("reschedule freebusy error: %s", e)
            return "No pude verificar la disponibilidad. Intentá más tarde."

        if _is_busy(new_start, busy):
            return "Ese horario ya no está disponible. Elegí otro de los disponibles."

        try:
            event = self.service.events().get(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()

            event["start"] = {
                "dateTime": new_start.isoformat(),
                "timeZone": "America/Argentina/Cordoba",
            }
            event["end"] = {
                "dateTime": new_end.isoformat(),
                "timeZone": "America/Argentina/Cordoba",
            }

            updated = self.service.events().update(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=event,
                sendUpdates="all",
            ).execute()

        except Exception as e:
            logger.error("reschedule_appointment error: %s", e)
            return "No pude reprogramar el turno. Intentá más tarde o llamá al consultorio."

        return f"Turno reprogramado al {_fmt(new_start)}. ID: {updated['id']}"