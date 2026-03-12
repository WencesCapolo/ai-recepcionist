"""
Orchestrates the full request lifecycle for a single incoming WhatsApp message.

Steps (in order):
  1. Extract message_id, user_phone, message_text (or media_id) from Meta payload.
  2. Dedup check.
  3. Identify client by inbound business number.
  3b. If audio message → transcribe via Whisper.
  4. Push message to debounce buffer.
  5. Poll until debounce window closes (no new message for 2s).
  6. Acquire per-conversation lock — only one handler drains the buffer.
  7. Drain buffer, combine into one input.
  8. Load history, run agent, send reply.
  9. Save history to Redis.
 10. Schedule background DB persistence.

All errors are handled internally — this function never raises.
"""

import asyncio
import logging
import time
import logfire
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks

from app.context.redis import conversation_context
from app.conversations.models import ConversationHistory, Message

logger = logging.getLogger(__name__)

FALLBACK_MESSAGE = (
    "Disculpa, tuve un problema con tu mensaje. "
    "Por favor mandame de nuevo en unos minutos, gracias!"
)

AUDIO_FALLBACK_MESSAGE = (
    "Disculpa, no pude escuchar el audio. Podrías escribirme lo que necesitás?"
)

# Maximum time (seconds) to wait for the debounce window to close.
# Safety ceiling — should never be hit under normal conditions.
DEBOUNCE_POLL_MAX = 10


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
            message_text: str = msg["text"]["body"]

            # If replying to a specific message, note it so the LLM has context.
            # The full original message is already in the conversation history.
            if msg.get("context"):
                message_text = f"[respondiendo a un mensaje anterior] {message_text}"

            return message_id, user_phone, message_text, inbound_number, None

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
    with logfire.span("dedup_check", message_id=message_id):
        if conversation_context.is_duplicate(message_id):
            logger.info(f"Duplicate message {message_id} — skipping.")
            return

    # ------------------------------------------------------------------
    # Step 3: Identify client
    # ------------------------------------------------------------------
    with logfire.span("client_lookup", inbound_number=inbound_number):
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

        with logfire.span("audio_transcription", media_id=media_id, client_id=client_id, user_phone=user_phone):
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
    # Step 4: Push to debounce buffer
    # ------------------------------------------------------------------
    # At this point message_text is guaranteed to be set:
    # text messages always have it; audio messages either got transcribed above or returned.
    assert message_text is not None, "message_text must be set before pushing to buffer"
    conversation_context.push_to_buffer(client_id, user_phone, message_id, message_text)

    # ------------------------------------------------------------------
    # Step 5: Wait for debounce window to close
    # Poll every 200ms. If a new message arrives it resets the 2s key in
    # Redis, so we keep waiting until 2s of silence has elapsed.
    # ------------------------------------------------------------------
    waited = 0.0
    while waited < DEBOUNCE_POLL_MAX:
        await asyncio.sleep(0.2)
        waited += 0.2
        if not conversation_context.is_debounce_active(client_id, user_phone):
            break  # 2s window closed — no newer message arrived

    # ------------------------------------------------------------------
    # Steps 6–10: Lock, drain buffer, run agent, reply, persist
    # ------------------------------------------------------------------
    with logfire.span("handle_message", user_phone=user_phone, client_id=client_id):
        try:
            async with conversation_context.lock(client_id, user_phone):

                # ----------------------------------------------------------
                # Drain buffer — may be 1 message or several
                # Another concurrent handler may have drained it already.
                # ----------------------------------------------------------
                buffered = conversation_context.drain_buffer(client_id, user_phone)
                if not buffered:
                    logger.debug(
                        f"Buffer already drained by another handler "
                        f"[client={client_id} user={user_phone}]"
                    )
                    return

                # Combine all buffered messages into one agent input
                if len(buffered) == 1:
                    combined_text = buffered[0]["text"]
                else:
                    combined_text = "\n".join(m["text"] for m in buffered)
                    logger.info(
                        f"Combined {len(buffered)} buffered messages "
                        f"[client={client_id} user={user_phone}]"
                    )

                # ----------------------------------------------------------
                # Load history
                # ----------------------------------------------------------
                history: ConversationHistory = conversation_context.load_history(
                    client_id, user_phone
                )

                # ----------------------------------------------------------
                # Run agent + send reply
                # ----------------------------------------------------------
                start_time = time.monotonic()
                reply: str = FALLBACK_MESSAGE
                tool_name: Optional[str] = None
                agent_succeeded = False

                try:
                    from app.agent.graph import run_agent

                    with logfire.span("agent_run", client_id=client_id, user_phone=user_phone):
                        reply, tool_name = await run_agent(
                            config=client_config,
                            history=history,
                            user_message=combined_text,
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
                    with logfire.span("whatsapp_send", user_phone=user_phone):
                        try:
                            await whatsapp_client.send_message(user_phone, reply)
                        except Exception as send_err:
                            logger.exception(
                                f"Failed to send WhatsApp reply to {user_phone}: {send_err}"
                            )

                latency_ms = int((time.monotonic() - start_time) * 1000)
                logger.info(
                    f"client={client_id} user={user_phone} "
                    f"messages={len(buffered)} latency={latency_ms}ms "
                    f"succeeded={agent_succeeded}"
                )

                # ----------------------------------------------------------
                # Save history to Redis
                # ----------------------------------------------------------
                try:
                    history = conversation_context.append_message(
                        history, Message(role="user", content=combined_text)
                    )
                    history = conversation_context.append_message(
                        history, Message(role="assistant", content=reply, tool_name=tool_name)
                    )
                    conversation_context.save_history(client_id, user_phone, history)
                except Exception:
                    logger.exception(
                        f"Failed to save history [client={client_id} user={user_phone}]"
                    )

                # ----------------------------------------------------------
                # Background DB persistence
                # ----------------------------------------------------------
                if conversation_service is not None:
                    background_tasks.add_task(
                        _persist,
                        conversation_service,
                        client_id,
                        user_phone,
                        combined_text,
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