# app/context/redis.py
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from upstash_redis import Redis

from app.conversations.models import Message, ConversationHistory
from app.config import settings

logger = logging.getLogger(__name__)

redis = Redis(url=settings.UPSTASH_REDIS_REST_URL, token=settings.UPSTASH_REDIS_REST_TOKEN)

HISTORY_TTL = 86400   # 24 hours — resets on each new message
LOCK_TTL = 10         # 10 seconds max per request cycle
DEDUP_TTL = 60        # 60 seconds — Meta sometimes resends the same webhook

def _history_key(client_id: str, user_phone: str) -> str:
    return f"history:{client_id}:{user_phone}"

def _lock_key(client_id: str, user_phone: str) -> str:
    return f"lock:{client_id}:{user_phone}"

def load_history(client_id: str, user_phone: str) -> ConversationHistory:
    """
    Load conversation history from Redis.
    Returns empty history on miss (Option A — fresh start after 24h inactivity).
    """
    key = _history_key(client_id, user_phone)
    try:
        raw = redis.get(key)
        if not raw:
            return ConversationHistory()
        data = json.loads(raw)
        return ConversationHistory(messages=[Message(**m) for m in data])
    except Exception as e:
        logger.error(f"Failed to load history for {client_id}:{user_phone}: {e}")
        return ConversationHistory()

def save_history(client_id: str, user_phone: str, history: ConversationHistory) -> None:
    """
    Save conversation history to Redis, resetting the 24h TTL.
    """
    key = _history_key(client_id, user_phone)
    try:
        payload = json.dumps([m.model_dump() for m in history.messages])
        redis.set(key, payload, ex=HISTORY_TTL)
    except Exception as e:
        logger.error(f"Failed to save history for {client_id}:{user_phone}: {e}")

def append_message(history: ConversationHistory, message: Message) -> ConversationHistory:
    """Pure helper — returns new history with message appended."""
    return ConversationHistory(messages=history.messages + [message])

@asynccontextmanager
async def conversation_lock(client_id: str, user_phone: str):
    """
    Async context manager for per-conversation Redis lock.
    Prevents race conditions when the same user sends multiple messages fast.

    Usage:
        async with conversation_lock(client_id, user_phone):
            # safe to read/write history here
    """
    lock_key = _lock_key(client_id, user_phone)
    acquired = False
    try:
        # SET NX — only sets if key doesn't exist
        acquired = redis.set(lock_key, "1", nx=True, ex=LOCK_TTL)
        if not acquired:
            logger.warning(f"Could not acquire lock for {client_id}:{user_phone} — message dropped")
            raise RuntimeError("Conversation locked — concurrent message in flight")
        yield
    finally:
        if acquired:
            try:
                redis.delete(lock_key)
            except Exception as e:
                logger.warning(f"Failed to release lock for {client_id}:{user_phone}: {e}")


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


class ConversationContext:
    def __init__(self, redis):
        self.redis = redis
