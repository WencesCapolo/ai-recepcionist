import json
import logging
from typing import Optional, cast

from upstash_redis import Redis
from supabase import Client

from app.clients.models import ClientConfig

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutes

# Flat Supabase columns that have been replaced by tool_config sub-models.
# _build_config strips these before passing data to ClientConfig so that Pydantic
# doesn't receive unknown fields.
_LEGACY_FLAT_FIELDS = frozenset({
    "sheet_id", "mp_access_token", "mp_sandbox", "calendar_id",
    "prices_sheet_id", "slot_minutes", "work_start_hour", "work_end_hour",
    "work_days", "sheet_tab_treatments", "sheet_tab_insurances", "sheet_tab_products",
})

# --- Per-client tool_config reference (until Supabase has a tool_config JSONB column) ---
#
# Ferretería Stainless:
#   retail:  {sheet_id: "1LPPx8pe250W4qVWxR_ROGBHapAy-PS0BMr8iQ-M43oo", tab: "productos"}
#   payment: {access_token: "APP_USR-...", sandbox: true}
#
# Consultorio Odontológico Martinez:
#   calendar: {calendar_id: "martinez@gmail.com", slot_minutes: 30, work_start: 9, work_end: 18}
#   dentist_sheets: {sheet_id: "...", tab_treatments: "Tratamientos", tab_insurances: "Obras Sociales"}
#   payment: {access_token: "...", sandbox: true}
#
# Tuti Bakery:
#   retail:  {sheet_id: "...", tab: "productos"}
#   payment: {access_token: "...", sandbox: true}
#
# Todo Padel:
#   padel:   {calendar_id: "...", slot_minutes: 60}
#   payment: {access_token: "...", sandbox: true}


def _build_config(data: dict) -> ClientConfig:
    """
    Maps a flat Supabase row to ClientConfig.

    If the row already has a populated tool_config JSONB column, strips legacy flat
    fields and passes it through. Otherwise builds tool_config from the legacy flat
    columns so existing clients work without a DB migration.
    """
    # Future path: tool_config JSONB column present and populated.
    if data.get("tool_config"):
        clean = {k: v for k, v in data.items() if k not in _LEGACY_FLAT_FIELDS}
        return ClientConfig(**clean)

    # Migration path: assemble tool_config from legacy flat columns.
    tools: list[str] = data.get("tools_enabled") or []
    tc: dict = {}

    if data.get("sheet_id"):
        tc["retail"] = {
            "sheet_id": data["sheet_id"],
            "tab": data.get("sheet_tab_products", "productos"),
        }

    if data.get("prices_sheet_id"):
        tc["dentist_sheets"] = {
            "sheet_id": data["prices_sheet_id"],
            "tab_treatments": data.get("sheet_tab_treatments", "Tratamientos"),
            "tab_insurances": data.get("sheet_tab_insurances", "Obras Sociales"),
        }

    if data.get("calendar_id") and any(t in tools for t in ("check_availability", "book_appointment")):
        tc["calendar"] = {
            "calendar_id": data.get("calendar_id"),
            "slot_minutes": data.get("slot_minutes", 30),
            "work_start": data.get("work_start_hour", 10),
            "work_end": data.get("work_end_hour", 18),
            "work_days": data.get("work_days", [0, 1, 2, 3, 4]),
        }

    if data.get("calendar_id") and "get_availability" in tools:
        tc["padel"] = {
            "calendar_id": data["calendar_id"],
            "slot_minutes": data.get("slot_minutes", 60),
        }

    if data.get("mp_access_token"):
        tc["payment"] = {
            "access_token": data["mp_access_token"],
            "sandbox": data.get("mp_sandbox", True),
        }

    clean = {k: v for k, v in data.items() if k not in _LEGACY_FLAT_FIELDS}
    clean["tool_config"] = tc
    return ClientConfig(**clean)


class ClientService:
    def __init__(self, supabase: Client, redis: Redis) -> None:
        self.supabase = supabase
        self.redis = redis

    async def get_client_by_phone(self, phone: str) -> Optional[ClientConfig]:
        """
        Load client config by their WhatsApp business number.
        Checks Redis cache first (5min TTL), falls back to Supabase.
        Returns None if no active client found for that number.
        """
        cache_key = f"config:{phone}"

        try:
            cached = self.redis.get(cache_key)
            if cached:
                logger.debug("Cache hit for client phone %s", phone)
                return ClientConfig(**json.loads(cached))
        except Exception as e:
            logger.warning("Redis cache read failed for %s: %s", phone, e)

        try:
            result = (
                self.supabase.table("clients")
                .select("*")
                .eq("whatsapp_number", phone)
                .eq("active", True)
                .maybe_single()
                .execute()
            )

            if result is None or not result.data:
                logger.warning("No active client found for phone %s", phone)
                return None

            config = _build_config(cast(dict, result.data))

            try:
                self.redis.set(cache_key, config.model_dump_json(), ex=CACHE_TTL)
            except Exception as e:
                logger.warning("Redis cache write failed for %s: %s", phone, e)

            return config

        except Exception as e:
            logger.error("Supabase lookup failed for phone %s: %s", phone, e)
            return None

    async def get_client_by_id(self, client_id: str) -> Optional[ClientConfig]:
        """Load client config by UUID. Used by mp_handler to fetch business name."""
        cache_key = f"config:id:{client_id}"

        try:
            cached = self.redis.get(cache_key)
            if cached:
                return ClientConfig(**json.loads(cached))
        except Exception as e:
            logger.warning("Redis cache read failed for client_id %s: %s", client_id, e)

        try:
            result = (
                self.supabase.table("clients")
                .select("*")
                .eq("id", client_id)
                .eq("active", True)
                .maybe_single()
                .execute()
            )

            if result is None or not result.data:
                logger.warning("No active client found for id %s", client_id)
                return None

            config = _build_config(cast(dict, result.data))

            try:
                self.redis.set(cache_key, config.model_dump_json(), ex=CACHE_TTL)
            except Exception as e:
                logger.warning("Redis cache write failed for client_id %s: %s", client_id, e)

            return config

        except Exception as e:
            logger.error("Supabase lookup failed for client_id %s: %s", client_id, e)
            return None

    async def get_any_mp_token(self) -> Optional[str]:
        """Return the MP access token from the first active client that has one."""
        try:
            result = (
                self.supabase.table("clients")
                .select("mp_access_token")
                .eq("active", True)
                .not_.is_("mp_access_token", "null")
                .limit(1)
                .execute()
            )
            if result and result.data:
                return cast(dict, result.data[0]).get("mp_access_token")
        except Exception as e:
            logger.error("Supabase lookup failed for get_any_mp_token: %s", e)
        return None

    def invalidate_client_cache(self, phone: str) -> None:
        """Call this if you update a client config and need immediate effect."""
        try:
            self.redis.delete(f"config:{phone}")
        except Exception as e:
            logger.warning("Cache invalidation failed for %s: %s", phone, e)
