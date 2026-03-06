import json
import logging
from typing import Optional

from upstash_redis import Redis

from app.clients.models import ClientConfig
from app.config import settings

logger = logging.getLogger(__name__)

redis = Redis(url=settings.UPSTASH_REDIS_REST_URL, token=settings.UPSTASH_REDIS_REST_TOKEN)

CACHE_TTL = 300  # 5 minutes


async def get_client_by_phone(phone: str) -> Optional[ClientConfig]:
    """
    Load client config by their WhatsApp business number.
    Checks Redis cache first (5min TTL), falls back to Supabase.
    Returns None if no active client found for that number.
    """
    cache_key = f"config:{phone}"

    # 1. Try Redis cache
    try:
        cached = redis.get(cache_key)
        if cached:
            logger.debug(f"Cache hit for client phone {phone}")
            return ClientConfig(**json.loads(cached))
    except Exception as e:
        logger.warning(f"Redis cache read failed for {phone}: {e}")

    # 2. Fall back to Supabase
    try:
        from supabase import create_client
        supabase = create_client(settings.supabase_url, settings.supabase_service_key)

        result = (
            supabase.table("clients")
            .select("*")
            .eq("whatsapp_number", phone)
            .eq("active", True)
            .single()
            .execute()
        )

        if not result.data:
            logger.warning(f"No active client found for phone {phone}")
            return None

        config = ClientConfig(**result.data)

        # 3. Write to cache
        try:
            redis.set(cache_key, config.model_dump_json(), ex=CACHE_TTL)
        except Exception as e:
            logger.warning(f"Redis cache write failed for {phone}: {e}")

        return config

    except Exception as e:
        logger.error(f"Supabase lookup failed for phone {phone}: {e}")
        return None


def invalidate_client_cache(phone: str) -> None:
    """Call this if you update a client config and need immediate effect."""
    try:
        redis.delete(f"config:{phone}")
    except Exception as e:
        logger.warning(f"Cache invalidation failed for {phone}: {e}")
