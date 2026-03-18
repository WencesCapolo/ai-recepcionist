"""
app/integrations/calendar_mock.py

CalendarMock — demo backend that stores appointments in Redis.
Exposes the exact same interface as GoogleCalendarClient so
calendar_tools.py works with either backend transparently.

Used when config.calendar_id is None/empty (demo clients).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

SLOT_MINUTES = 30
WORK_START   = 10
WORK_END     = 18
WORK_DAYS    = {0, 1, 2, 3, 4}
ART          = timezone(timedelta(hours=-3))

# Pre-blocked slots for demo realism (relative offsets in hours from now)
_DEMO_BUSY_OFFSETS = [3, 5, 27, 28]


def _fmt(dt: datetime) -> str:
    art    = dt.astimezone(ART)
    days   = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    months = [
        "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    return f"{days[art.weekday()]} {art.day} de {months[art.month]} a las {art.strftime('%H:%M')}"


def _next_candidates(n: int) -> list[datetime]:
    now       = datetime.now(tz=ART)
    start     = now + timedelta(hours=2)
    remainder = start.minute % SLOT_MINUTES
    if remainder:
        start += timedelta(minutes=(SLOT_MINUTES - remainder))
    start = start.replace(second=0, microsecond=0)

    slots, cursor = [], start
    while len(slots) < n * 3:
        if cursor.weekday() in WORK_DAYS and WORK_START <= cursor.hour < WORK_END:
            slots.append(cursor)
        cursor += timedelta(minutes=SLOT_MINUTES)
        if cursor.hour >= WORK_END:
            cursor = (cursor + timedelta(days=1)).replace(
                hour=WORK_START, minute=0, second=0
            )
        while cursor.weekday() not in WORK_DAYS:
            cursor += timedelta(days=1)
    return slots


class CalendarMock:
    def __init__(self, redis, client_id: str):
        self.redis     = redis
        self.client_id = client_id

    # ── Internal keys ─────────────────────────────────────────────────────────

    def _appt_key(self, event_id: str) -> str:
        return f"mock_appt:{self.client_id}:{event_id}"

    def _lock_key(self, slot_iso: str) -> str:
        return f"slot_lock:{self.client_id}:{slot_iso}"

    def _index_key(self) -> str:
        return f"mock_appt_index:{self.client_id}"

    def _acquire_lock(self, slot_iso: str, owner: str) -> bool:
        return bool(self.redis.set(self._lock_key(slot_iso), owner, nx=True, ex=300))

    def _slot_locked(self, slot_iso: str) -> bool:
        return bool(self.redis.exists(self._lock_key(slot_iso)))

    def _demo_busy(self) -> set[str]:
        """Slots pre-blocked for demo realism."""
        now = datetime.now(tz=ART)
        busy = set()
        for offset in _DEMO_BUSY_OFFSETS:
            candidate = now + timedelta(hours=offset)
            remainder = candidate.minute % SLOT_MINUTES
            if remainder:
                candidate += timedelta(minutes=(SLOT_MINUTES - remainder))
            candidate = candidate.replace(second=0, microsecond=0)
            busy.add(candidate.isoformat())
        return busy

    # ── Public interface ───────────────────────────────────────────────────────

    def get_current_date_hour(self) -> str:
        now    = datetime.now(tz=ART)
        days   = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        months = [
            "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        return (
            f"Hoy es {days[now.weekday()]} {now.day} de {months[now.month]} de {now.year}. "
            f"Son las {now.strftime('%H:%M')} (hora Argentina)."
        )

    def check_availability(self, count: int = 3) -> str:
        candidates = _next_candidates(count)
        demo_busy  = self._demo_busy()

        # Also exclude slots already booked in Redis mock store
        raw_index = self.redis.get(self._index_key())
        booked_slots: set[str] = set()
        if raw_index:
            index = json.loads(raw_index)
            for event_id in index:
                raw = self.redis.get(self._appt_key(event_id))
                if raw:
                    appt = json.loads(raw)
                    booked_slots.add(appt.get("slot_iso", ""))

        available = [
            s for s in candidates
            if s.isoformat() not in demo_busy
            and s.isoformat() not in booked_slots
            and not self._slot_locked(s.isoformat())
        ][:count]

        if not available:
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
    ) -> str:
        owner = f"{patient_phone}:{slot_iso}"
        if not self._acquire_lock(slot_iso, owner):
            return "Ese horario acaba de ser tomado. Elegí otro de los disponibles."

        event_id = str(uuid.uuid4())[:8]
        slot_dt  = datetime.fromisoformat(slot_iso)

        appt = {
            "event_id":      event_id,
            "patient_name":  patient_name,
            "patient_phone": patient_phone,
            "patient_email": patient_email,
            "reason":        reason,
            "slot_iso":      slot_iso,
            "is_new_patient": is_new_patient,
        }
        self.redis.set(self._appt_key(event_id), json.dumps(appt), ex=60 * 60 * 24 * 30)

        # Update index
        raw_index = self.redis.get(self._index_key())
        index     = json.loads(raw_index) if raw_index else []
        index.append(event_id)
        self.redis.set(self._index_key(), json.dumps(index), ex=60 * 60 * 24 * 30)

        logger.info(
            "Mock appointment booked: %s | %s | %s | %s",
            event_id, patient_name, slot_iso, reason,
        )

        return (
            f"Turno confirmado: {patient_name}, {_fmt(slot_dt)}, motivo: {reason}. "
            f"Se enviará confirmación a {patient_email}. "
            f"ID: {event_id}"
        )

    def get_appointment(self, patient_name: str, date_hint: Optional[str] = None) -> str:
        raw_index = self.redis.get(self._index_key())
        if not raw_index:
            return f"No encontré turnos para {patient_name}."

        index  = json.loads(raw_index)
        found  = []
        now    = datetime.now(tz=ART)
        q      = patient_name.lower()

        for event_id in index:
            raw = self.redis.get(self._appt_key(event_id))
            if not raw:
                continue
            appt     = json.loads(raw)
            slot_dt  = datetime.fromisoformat(appt["slot_iso"])
            if slot_dt < now:
                continue  # skip past appointments
            if q.split()[0] in appt["patient_name"].lower():
                found.append((slot_dt, appt, event_id))

        if not found:
            return f"No encontré turnos próximos para {patient_name}."

        found.sort(key=lambda x: x[0])
        lines = [
            f"- {_fmt(dt)}: {appt['patient_name']} — {appt['reason']}  [ID: {eid}]"
            for dt, appt, eid in found
        ]
        return f"Turnos encontrados para {patient_name}:\n" + "\n".join(lines)

    def cancel_appointment(self, event_id: str, patient_name: str) -> str:
        raw = self.redis.get(self._appt_key(event_id))
        if not raw:
            return "No encontré ese turno. Verificá el ID."

        appt = json.loads(raw)
        if patient_name.lower().split()[0] not in appt["patient_name"].lower():
            return f"El turno no corresponde a {patient_name}. Verificá."

        slot_dt = datetime.fromisoformat(appt["slot_iso"])
        self.redis.delete(self._appt_key(event_id))

        # Remove from index
        raw_index = self.redis.get(self._index_key())
        if raw_index:
            index = [e for e in json.loads(raw_index) if e != event_id]
            self.redis.set(self._index_key(), json.dumps(index), ex=60 * 60 * 24 * 30)

        return f"Turno cancelado: {_fmt(slot_dt)} para {patient_name}."

    def reschedule_appointment(self, event_id: str, new_slot_iso: str) -> str:
        raw = self.redis.get(self._appt_key(event_id))
        if not raw:
            return "No encontré ese turno. Verificá el ID."

        if self._slot_locked(new_slot_iso):
            return "Ese horario ya fue tomado. Elegí otro de los disponibles."

        appt           = json.loads(raw)
        old_slot       = appt["slot_iso"]
        appt["slot_iso"] = new_slot_iso
        self.redis.set(self._appt_key(event_id), json.dumps(appt), ex=60 * 60 * 24 * 30)

        new_dt = datetime.fromisoformat(new_slot_iso)
        return f"Turno reprogramado al {_fmt(new_dt)}. ID: {event_id}"