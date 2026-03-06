# app/dedup.py
import logging

from upstash_redis import Redis
from app.config import settings

logger = logging.getLogger(__name__)

redis = Redis(url=settings.UPSTASH_REDIS_REST_URL, token=settings.UPSTASH_REDIS_REST_TOKEN)

DEDUP_TTL = 60  # 60 seconds — Meta sometimes resends the same webhook


def is_duplicate(message_id: str) -> bool:
    """
    Returns True if this message_id was already processed within the last 60s.
    Uses Redis SET NX — atomic, safe under concurrent requests.
    """
    key = f"dedup:{message_id}"
    try:
        inserted = redis.set(key, "1", nx=True, ex=DEDUP_TTL)
        # SET NX returns True if key was newly set, None/False if already existed
        return not inserted
    except Exception as e:
        # On Redis failure, allow the message through (fail open)
        logger.error(f"Dedup Redis check failed for {message_id}: {e}")
        return False