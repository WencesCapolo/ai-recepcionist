# app/context/redis.py
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

from upstash_redis import Redis

from app.conversations.models import Message, ConversationHistory
from app.config import settings

logger = logging.getLogger(__name__)

HISTORY_TTL = 86400   # 24 hours — resets on each new message
LOCK_TTL = 10         # 10 seconds max per request cycle
DEDUP_TTL = 60        # 60 seconds — Meta sometimes resends the same webhook
DEBOUNCE_TTL = 3      # key expires after 3s of inactivity — the debounce window
BUFFER_TTL = 30       # max buffer lifetime — safety ceiling


class ConversationContext:
    """
    All per-conversation Redis state: history, locking, dedup, debounce buffer.
    Instantiate once and inject via FastAPI Depends.
    """

    def __init__(self, redis_client: Redis) -> None:
        self._redis = redis_client

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _history_key(self, client_id: str, user_phone: str) -> str:
        return f"history:{client_id}:{user_phone}"

    def _lock_key(self, client_id: str, user_phone: str) -> str:
        return f"lock:{client_id}:{user_phone}"

    def _buffer_key(self, client_id: str, user_phone: str) -> str:
        return f"buffer:{client_id}:{user_phone}"

    def _debounce_key(self, client_id: str, user_phone: str) -> str:
        return f"debounce:{client_id}:{user_phone}"

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def load_history(self, client_id: str, user_phone: str) -> ConversationHistory:
        """
        Load conversation history from Redis.
        Returns empty history on miss (fresh start after 24h inactivity).
        """
        key = self._history_key(client_id, user_phone)
        try:
            raw = self._redis.get(key)
            if not raw:
                return ConversationHistory()
            data = json.loads(raw)
            return ConversationHistory(messages=[Message(**m) for m in data])
        except Exception as e:
            logger.error(f"Failed to load history [{client_id}:{user_phone}]: {e}")
            return ConversationHistory()

    def save_history(
        self, client_id: str, user_phone: str, history: ConversationHistory
    ) -> None:
        """Save conversation history to Redis, resetting the 24h TTL."""
        key = self._history_key(client_id, user_phone)
        try:
            payload = json.dumps([m.model_dump() for m in history.messages])
            self._redis.set(key, payload, ex=HISTORY_TTL)
        except Exception as e:
            logger.error(f"Failed to save history [{client_id}:{user_phone}]: {e}")

    @staticmethod
    def append_message(
        history: ConversationHistory, message: Message
    ) -> ConversationHistory:
        """Pure helper — returns new history with message appended."""
        return ConversationHistory(messages=history.messages + [message])

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def is_duplicate(self, message_id: str) -> bool:
        """
        Returns True if this message_id was already processed within the last 60s.
        Uses Redis SET NX — atomic, safe under concurrent requests.
        """
        key = f"dedup:{message_id}"
        try:
            inserted = self._redis.set(key, "1", nx=True, ex=DEDUP_TTL)
            # SET NX returns True if key was newly set, None/False if already existed
            return not inserted
        except Exception as e:
            # On Redis failure, allow the message through (fail open)
            logger.error(f"Dedup Redis check failed for {message_id}: {e}")
            return False

    # ------------------------------------------------------------------
    # Debounce buffer
    # ------------------------------------------------------------------

    def push_to_buffer(
        self,
        client_id: str,
        user_phone: str,
        message_id: str,
        text: str,
    ) -> None:
        """
        Append a message to the user's pending buffer and reset the debounce window.
        """
        buffer_key = self._buffer_key(client_id, user_phone)
        debounce_key = self._debounce_key(client_id, user_phone)

        # Store as JSON so we preserve message_id alongside text
        entry = json.dumps({"id": message_id, "text": text})
        self._redis.rpush(buffer_key, entry)
        self._redis.expire(buffer_key, BUFFER_TTL)

        # Reset debounce window — this is what callers poll against
        self._redis.set(debounce_key, "1", ex=DEBOUNCE_TTL)

    def is_debounce_active(self, client_id: str, user_phone: str) -> bool:
        """Returns True if we're still within the debounce window."""
        return bool(self._redis.exists(self._debounce_key(client_id, user_phone)))

    def drain_buffer(self, client_id: str, user_phone: str) -> list[dict]:
        """
        Atomically read and delete all buffered messages.
        Returns list of {id, text} dicts in arrival order.
        """
        buffer_key = self._buffer_key(client_id, user_phone)
        pipe = self._redis.pipeline()
        pipe.lrange(buffer_key, 0, -1)
        pipe.delete(buffer_key)
        results = pipe.execute()  # type: ignore[call-arg]
        raw_messages = results[0] or []
        return [json.loads(m) for m in raw_messages]

    # ------------------------------------------------------------------
    # Per-conversation lock (retry with backoff)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def lock(self, client_id: str, user_phone: str):
        """
        Per-conversation lock with retry.
        Waits up to 12 seconds for the lock to free before giving up.
        This handles fast consecutive messages without dropping them.

        Usage:
            async with ctx.lock(client_id, user_phone):
                # safe to read/write history here
        """
        lock_key = self._lock_key(client_id, user_phone)
        acquired = False

        # Retry every 500ms for up to 12 seconds (covers a full agent turn)
        max_attempts = 24
        for attempt in range(max_attempts):
            acquired = self._redis.set(lock_key, "1", nx=True, ex=LOCK_TTL)
            if acquired:
                break
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5)

        if not acquired:
            raise RuntimeError(
                f"Lock timeout [{client_id}:{user_phone}] after {max_attempts * 0.5}s"
            )

        try:
            yield
        finally:
            if acquired:
                try:
                    self._redis.delete(lock_key)
                except Exception as e:
                    logger.warning(f"Lock release failed [{client_id}:{user_phone}]: {e}")


# ---------------------------------------------------------------------------
# Module-level singleton — used by handler.py and injected via FastAPI Depends
# ---------------------------------------------------------------------------

_redis_client = Redis(
    url=settings.UPSTASH_REDIS_REST_URL,
    token=settings.UPSTASH_REDIS_REST_TOKEN,
)

conversation_context = ConversationContext(_redis_client)

# ---------------------------------------------------------------------------
# Module-level shims — kept for callers that haven't migrated to the class yet
# ---------------------------------------------------------------------------

def load_history(client_id: str, user_phone: str) -> ConversationHistory:
    return conversation_context.load_history(client_id, user_phone)

def save_history(client_id: str, user_phone: str, history: ConversationHistory) -> None:
    conversation_context.save_history(client_id, user_phone, history)

def append_message(history: ConversationHistory, message: Message) -> ConversationHistory:
    return ConversationContext.append_message(history, message)

def is_duplicate(message_id: str) -> bool:
    return conversation_context.is_duplicate(message_id)
