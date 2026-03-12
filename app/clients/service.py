import json
import logging
from typing import Optional, cast

from upstash_redis import Redis
from supabase import Client

from app.clients.models import ClientConfig

logger = logging.getLogger(__name__)

CACHE_TTL = 300  # 5 minutes

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

        # 1. Try Redis cache
        try:
            cached = self.redis.get(cache_key)
            if cached:
                logger.debug(f"Cache hit for client phone {phone}")
                return ClientConfig(**json.loads(cached))
        except Exception as e:
            logger.warning(f"Redis cache read failed for {phone}: {e}")

        # 2. Fall back to Supabase
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
                logger.warning(f"No active client found for phone {phone}")
                return None

            data = cast(dict, result.data)
            config = ClientConfig(**data)

            # 3. Write to cache
            try:
                self.redis.set(cache_key, config.model_dump_json(), ex=CACHE_TTL)
            except Exception as e:
                logger.warning(f"Redis cache write failed for {phone}: {e}")

            return config

        except Exception as e:
            logger.error(f"Supabase lookup failed for phone {phone}: {e}")
            return None

    def invalidate_client_cache(self, phone: str) -> None:
        """Call this if you update a client config and need immediate effect."""
        try:
            self.redis.delete(f"config:{phone}")
        except Exception as e:
            logger.warning(f"Cache invalidation failed for {phone}: {e}")
