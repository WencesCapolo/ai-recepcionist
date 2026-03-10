"""
app/conversations/service.py
Supabase read/write operations for the conversations and messages tables.
This service is ONLY called as a FastAPI BackgroundTask — it must never be
awaited in the hot path and must never let exceptions propagate.
"""
import logging
import uuid
from datetime import datetime, timezone

from supabase import Client

from app.conversations.models import Message

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, supabase: Client) -> None:
        self._db = supabase

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upsert_conversation(self, client_id: str, user_phone: str) -> str:
        """
        Find the existing conversation for (client_id, user_phone) or create a
        new one.  Always bumps last_message_at to now().
        Returns the conversation_id as a string, or "" on failure.
        """
        try:
            # Ensure client_id is always a plain string — UUID objects are not
            # JSON-serialisable and will cause a TypeError inside supabase-py.
            client_id = str(client_id)
            now = datetime.now(timezone.utc).isoformat()

            response = (
                self._db.table("conversations")
                .select("id")
                .eq("client_id", client_id)
                .eq("user_phone", user_phone)
                .order("started_at", desc=True)
                .limit(1)
                .execute()
            )

            if response.data:
                conversation_id: str = response.data[0]["id"]
                self._db.table("conversations").update(
                    {"last_message_at": now}
                ).eq("id", conversation_id).execute()
            else:
                conversation_id = str(uuid.uuid4())
                self._db.table("conversations").insert(
                    {
                        "id": conversation_id,
                        "client_id": client_id,
                        "user_phone": user_phone,
                        "started_at": now,
                        "last_message_at": now,
                    }
                ).execute()

            return conversation_id

        except Exception:
            logger.exception(
                "upsert_conversation failed [client_id=%s user_phone=%s]",
                client_id,
                user_phone,
            )
            return ""

    async def log_messages(
        self,
        conversation_id: str,
        messages: list[Message],
        prompt_version: int,
        latency_ms: int,
    ) -> None:
        """
        Insert all messages from the list into the messages table.
        prompt_version and latency_ms are only set on the assistant message.
        Silently swallows any exception — this is a background task.
        """
        try:
            rows = []
            for msg in messages:
                row: dict = {
                    "id": str(uuid.uuid4()),
                    "conversation_id": conversation_id,
                    "role": msg.role,
                    "content": msg.content,
                    "tool_name": msg.tool_name,
                }
                if msg.role == "assistant":
                    row["prompt_version"] = prompt_version
                    row["latency_ms"] = latency_ms
                rows.append(row)

            if rows:
                self._db.table("messages").insert(rows).execute()

        except Exception:
            logger.exception(
                "log_messages failed for conversation_id=%s", conversation_id
            )