# app/webhook/handler.py
"""
Orchestrates the full request lifecycle for a single incoming WhatsApp message.

Steps (in order):
  1. Extract message_id, user_phone, message_text from Meta payload.
  2. Dedup check.
  3. Identify client by inbound business number.
  4. Acquire per-conversation lock.
  5. Load conversation history from Redis.
  6. Record start time.
  7. Run agent → get reply.
  8. Send reply via WhatsApp.
  9. Append messages to history, persist to Redis.
 10. Schedule background DB tasks.

All errors are handled internally — this function never raises.
"""

import logging
import time
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks

from app.context.redis import (
    append_message,
    conversation_lock,
    is_duplicate,
    load_history,
    save_history,
)
from app.conversations.models import ConversationHistory, Message

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = (
    "Lo siento, tuve un problema para procesar tu mensaje. "
    "Por favor intentá de nuevo en unos minutos 🙏"
)


# ---------------------------------------------------------------------------
# Payload parsing helpers
# ---------------------------------------------------------------------------

def _extract_message_fields(payload: dict) -> Optional[tuple[str, str, str, str]]:
    """
    Parse a Meta Cloud API webhook payload.

    Returns (message_id, user_phone, message_text, inbound_number) or None
    if the payload doesn't contain a valid inbound text message.
    """
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]

        # Must have a messages array with at least one item
        messages = change.get("messages")
        if not messages:
            return None

        msg = messages[0]

        # Only handle text messages
        if msg.get("type") != "text":
            return None

        message_id: str = msg["id"]
        user_phone: str = msg["from"]
        message_text: str = msg["text"]["body"]

        # The business number that received the message
        inbound_number: str = change["metadata"]["display_phone_number"]

        return message_id, user_phone, message_text, inbound_number

    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Main lifecycle function
# ---------------------------------------------------------------------------

async def handle_message(
    payload: dict,
    background_tasks: BackgroundTasks,
    # Injected dependencies — callables / client objects
    get_client_by_phone: Callable,          # async (phone: str) -> ClientConfig | None
    conversation_service: Any,              # has .upsert_conversation() and .log_messages()
    whatsapp_client: Any,                   # has async .send_message(to, text)
    sheets_client: Any,                     # passed straight through to run_agent
) -> None:
    """
    Handle a single inbound WhatsApp message end-to-end.

    Never raises — all errors are logged and, where possible, a Spanish
    fallback message is sent to the user.
    """

    # ------------------------------------------------------------------
    # Step 1: Extract fields from the Meta webhook payload
    # ------------------------------------------------------------------
    parsed = _extract_message_fields(payload)
    if parsed is None:
        logger.debug("Payload contains no valid text message — skipping.")
        return

    message_id, user_phone, message_text, inbound_number = parsed

    # ------------------------------------------------------------------
    # Step 2: Deduplication
    # ------------------------------------------------------------------
    if is_duplicate(message_id):
        logger.info(f"Duplicate message {message_id} — skipping.")
        return

    # ------------------------------------------------------------------
    # Step 3: Identify client by their WhatsApp business number
    # ------------------------------------------------------------------
    client_config = await get_client_by_phone(inbound_number)
    if client_config is None:
        logger.warning(f"No active client found for inbound number {inbound_number} — skipping.")
        return

    client_id: str = client_config.id

    # ------------------------------------------------------------------
    # Step 4: Acquire per-conversation lock
    # ------------------------------------------------------------------
    try:
        async with conversation_lock(client_id, user_phone):

            # ----------------------------------------------------------
            # Step 5: Load conversation history
            # ----------------------------------------------------------
            history: ConversationHistory = load_history(client_id, user_phone)

            # ----------------------------------------------------------
            # Step 6: Record start time for latency tracking
            # ----------------------------------------------------------
            start_time = time.monotonic()

            # ----------------------------------------------------------
            # Steps 7–8: Run agent + send reply (errors caught together)
            # ----------------------------------------------------------
            reply: str = FALLBACK_MESSAGE  # safe default
            agent_succeeded = False

            try:
                # Lazy import to avoid circular deps at module load time
                from app.agent.graph import run_agent

                reply = await run_agent(
                    config=client_config,
                    history=history,
                    user_message=message_text,
                    sheets=sheets_client,
                )
                agent_succeeded = True

            except Exception as agent_err:
                logger.exception(
                    f"Agent error for client={client_id} user={user_phone}: {agent_err}"
                )
                reply = FALLBACK_MESSAGE

            finally:
                # Step 8: Always attempt to send something to the user
                try:
                    await whatsapp_client.send_message(user_phone, reply)
                except Exception as send_err:
                    logger.exception(
                        f"Failed to send WhatsApp reply to {user_phone}: {send_err}"
                    )

            # Latency measured from start through send completion
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.info(
                f"client={client_id} user={user_phone} "
                f"latency={latency_ms:.0f}ms succeeded={agent_succeeded}"
            )

            # ----------------------------------------------------------
            # Step 9: Persist updated history to Redis
            # ----------------------------------------------------------
            try:
                user_msg = Message(role="user", content=message_text)
                assistant_msg = Message(role="assistant", content=reply)
                history = append_message(history, user_msg)
                history = append_message(history, assistant_msg)
                save_history(client_id, user_phone, history)
            except Exception as history_err:
                logger.exception(
                    f"Failed to save history for client={client_id} user={user_phone}: {history_err}"
                )

            # ----------------------------------------------------------
            # Step 10: Schedule background DB tasks
            # ----------------------------------------------------------
            if conversation_service is not None:
                background_tasks.add_task(
                    conversation_service.upsert_conversation,
                    client_id=client_id,
                    user_phone=user_phone,
                )
                background_tasks.add_task(
                    conversation_service.log_messages,
                    client_id=client_id,
                    user_phone=user_phone,
                    user_message=message_text,
                    assistant_message=reply,
                )

    except RuntimeError as lock_err:
        # Lock already held — a concurrent message is in flight for this user
        logger.warning(
            f"Lock contention for client={client_id} user={user_phone}: {lock_err}"
        )
        return

    except Exception as unexpected_err:
        # Catch-all: should never reach here, but log and swallow
        logger.exception(
            f"Unexpected error in handle_message for client={client_id} "
            f"user={user_phone}: {unexpected_err}"
        )
