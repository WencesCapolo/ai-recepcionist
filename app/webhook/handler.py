"""
Orchestrates the full request lifecycle for a single incoming WhatsApp message.

Steps (in order):
  1. Extract message_id, user_phone, message_text (or media_id) from Meta payload.
  2. Dedup check.
  3. Identify client by inbound business number.
  3b. If audio message → transcribe via Whisper.
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
    "Disculpa, tuve un problema con tu mensaje. "
    "Por favor mandame de nuevo en unos minutos, gracias!"
)

AUDIO_FALLBACK_MESSAGE = (
    "Disculpa, no pude escuchar el audio. Podrías escribirme lo que necesitás?"
)


# ---------------------------------------------------------------------------
# Payload parsing helpers
# ---------------------------------------------------------------------------

def _extract_message_fields(
    payload: dict,
) -> Optional[tuple[str, str, Optional[str], str, Optional[str]]]:
    """
    Parse a Meta Cloud API webhook payload.

    Returns (message_id, user_phone, message_text, inbound_number, media_id)
    or None if the payload doesn't contain a supported inbound message.

    - For text messages: message_text is set, media_id is None.
    - For audio messages: message_text is None, media_id is set.
    """
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]

        messages = change.get("messages")
        if not messages:
            return None

        msg = messages[0]
        msg_type = msg.get("type")

        message_id: str = msg["id"]
        user_phone: str = msg["from"]
        inbound_number: str = change["metadata"]["display_phone_number"]

        if msg_type == "text":
            return message_id, user_phone, msg["text"]["body"], inbound_number, None

        if msg_type == "audio":
            media_id: str = msg["audio"]["id"]
            return message_id, user_phone, None, inbound_number, media_id

        # Unsupported message type
        return None

    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Background persistence — runs after reply is sent, never raises
# ---------------------------------------------------------------------------

async def _persist(
    conversation_service: Any,
    client_id: str,
    user_phone: str,
    user_message: str,
    assistant_reply: str,
    tool_name: Optional[str],
    prompt_version: int,
    latency_ms: int,
) -> None:
    """
    Upsert the conversation row, then log both messages.
    Runs as a BackgroundTask — must never raise.
    """
    try:
        conversation_id = await conversation_service.upsert_conversation(
            client_id=client_id,
            user_phone=user_phone,
        )
        if not conversation_id:
            logger.error(
                f"_persist: upsert_conversation returned empty id "
                f"[client={client_id} user={user_phone}] — skipping log_messages"
            )
            return

        messages = [
            Message(role="user", content=user_message),
            Message(role="assistant", content=assistant_reply, tool_name=tool_name),
        ]
        await conversation_service.log_messages(
            conversation_id=conversation_id,
            messages=messages,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
        )
    except Exception:
        logger.exception(
            f"_persist failed [client={client_id} user={user_phone}]"
        )


# ---------------------------------------------------------------------------
# Main lifecycle function
# ---------------------------------------------------------------------------

async def handle_message(
    payload: dict,
    background_tasks: BackgroundTasks,
    get_client_by_phone: Callable,   # async (phone: str) -> ClientConfig | None
    conversation_service: Any,       # ConversationService instance
    whatsapp_client: Any,            # WhatsAppClient instance
    sheets_client: Any,              # SheetsClient instance
    transcriber_client: Any = None,  # TranscriberClient instance (optional)
) -> None:
    """
    Handle a single inbound WhatsApp message end-to-end.
    Never raises — all errors are logged and a Spanish fallback is sent when possible.
    """

    # ------------------------------------------------------------------
    # Step 1: Parse payload
    # ------------------------------------------------------------------
    parsed = _extract_message_fields(payload)
    if parsed is None:
        logger.debug("Payload contains no supported message — skipping.")
        return

    message_id, user_phone, message_text, inbound_number, media_id = parsed

    # ------------------------------------------------------------------
    # Step 2: Deduplication
    # ------------------------------------------------------------------
    if is_duplicate(message_id):
        logger.info(f"Duplicate message {message_id} — skipping.")
        return

    # ------------------------------------------------------------------
    # Step 3: Identify client
    # ------------------------------------------------------------------
    client_config = await get_client_by_phone(inbound_number)
    if client_config is None:
        logger.warning(f"No active client for inbound number {inbound_number} — skipping.")
        return

    client_id: str = str(client_config.id)

    # ------------------------------------------------------------------
    # Step 3b: Transcribe audio (if applicable)
    # ------------------------------------------------------------------
    if message_text is None and media_id is not None:
        if transcriber_client is None:
            logger.error("Audio message received but no transcriber_client provided.")
            try:
                await whatsapp_client.send_message(user_phone, AUDIO_FALLBACK_MESSAGE)
            except Exception:
                logger.exception(f"Failed to send audio fallback to {user_phone}")
            return

        try:
            message_text = await transcriber_client.transcribe(media_id)
            logger.info(
                f"Audio transcribed [client={client_id} user={user_phone}]: "
                f"{len(message_text)} chars"
            )
        except Exception as transcribe_err:
            logger.exception(
                f"Transcription failed [client={client_id} user={user_phone}]: "
                f"{transcribe_err}"
            )
            try:
                await whatsapp_client.send_message(user_phone, AUDIO_FALLBACK_MESSAGE)
            except Exception:
                logger.exception(f"Failed to send audio fallback to {user_phone}")
            return

    # ------------------------------------------------------------------
    # Step 4: Acquire per-conversation lock
    # ------------------------------------------------------------------
    try:
        async with conversation_lock(client_id, user_phone):

            # ----------------------------------------------------------
            # Step 5: Load history
            # ----------------------------------------------------------
            history: ConversationHistory = load_history(client_id, user_phone)

            # ----------------------------------------------------------
            # Step 6: Start timer
            # ----------------------------------------------------------
            start_time = time.monotonic()

            # ----------------------------------------------------------
            # Steps 7–8: Run agent + send reply
            # ----------------------------------------------------------
            reply: str = FALLBACK_MESSAGE
            tool_name: Optional[str] = None
            agent_succeeded = False

            try:
                from app.agent.graph import run_agent

                reply, tool_name = await run_agent(
                    config=client_config,
                    history=history,
                    user_message=message_text,
                    sheets=sheets_client,
                )
                agent_succeeded = True

            except Exception as agent_err:
                logger.exception(
                    f"Agent error [client={client_id} user={user_phone}]: {agent_err}"
                )
                reply = FALLBACK_MESSAGE
                tool_name = None

            finally:
                try:
                    await whatsapp_client.send_message(user_phone, reply)
                except Exception as send_err:
                    logger.exception(
                        f"Failed to send WhatsApp reply to {user_phone}: {send_err}"
                    )

            latency_ms = int((time.monotonic() - start_time) * 1000)
            logger.info(
                f"client={client_id} user={user_phone} "
                f"latency={latency_ms}ms succeeded={agent_succeeded}"
            )

            # ----------------------------------------------------------
            # Step 9: Save history to Redis
            # ----------------------------------------------------------
            try:
                history = append_message(history, Message(role="user", content=message_text))
                history = append_message(history, Message(role="assistant", content=reply, tool_name=tool_name))
                save_history(client_id, user_phone, history)
            except Exception:
                logger.exception(
                    f"Failed to save history [client={client_id} user={user_phone}]"
                )

            # ----------------------------------------------------------
            # Step 10: Background DB persistence
            # ----------------------------------------------------------
            if conversation_service is not None:
                background_tasks.add_task(
                    _persist,
                    conversation_service,
                    client_id,
                    user_phone,
                    message_text,
                    reply,
                    tool_name,
                    client_config.prompt_version,
                    latency_ms,
                )

    except RuntimeError as lock_err:
        logger.warning(
            f"Lock contention [client={client_id} user={user_phone}]: {lock_err}"
        )

    except Exception:
        logger.exception(
            f"Unexpected error in handle_message [client={client_id} user={user_phone}]"
        )