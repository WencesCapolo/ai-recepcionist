from datetime import date, datetime, timedelta, timezone

ART = timezone(timedelta(hours=-3))

DAYS_ES: list[str] = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"
]

MONTHS_ES: list[str] = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
]

def fmt_datetime(dt: datetime) -> str:
    art = dt.astimezone(ART)
    return f"{DAYS_ES[art.weekday()]} {art.day} de {MONTHS_ES[art.month]} a las {art.strftime('%H:%M')}"

def fmt_date(d: date) -> str:
    return f"{DAYS_ES[d.weekday()]} {d.day} de {MONTHS_ES[d.month]} de {d.year}"

def now_art() -> datetime:
    return datetime.now(tz=ART)
