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
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks

from app.agent.graph import run_agent
from app.clients.models import ClientConfig
from app.context.redis import ConversationContext
from app.conversations.models import ConversationHistory, Message
from app.webhook.payload_parser import parse_payload, ParsedMessage

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


@dataclass
class TurnResult:
    reply: str
    latency_ms: int
    messages: list[Message]
    tool_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Extracted single-responsibility steps
# ---------------------------------------------------------------------------

async def _resolve_client(inbound_number: str, get_client_by_phone: Callable) -> Optional[ClientConfig]:
    """Retrieve client config by inbound phone number."""
    with logfire.span("client_lookup", inbound_number=inbound_number):
        client_config = await get_client_by_phone(inbound_number)
        if client_config is None:
            logger.warning(f"No active client for inbound number {inbound_number} — skipping.")
        return client_config


async def _resolve_text(
    parsed: ParsedMessage,
    client_id: str,
    transcriber_client: Any,
    whatsapp_client: Any, 
) -> Optional[str]:
    """Return text if present, otherwise transcribe audio. Handle fallbacks."""
    if parsed.message_text is not None:
        return parsed.message_text

    if parsed.media_id is None:
        return None

    if transcriber_client is None:
        logger.error("Audio message received but no transcriber_client provided.")
        try:
            await whatsapp_client.send_message(parsed.user_phone, AUDIO_FALLBACK_MESSAGE)
        except Exception:
            logger.exception(f"Failed to send audio fallback to {parsed.user_phone}")
        return None

    with logfire.span("audio_transcription", media_id=parsed.media_id, client_id=client_id, user_phone=parsed.user_phone):
        try:
            message_text = await transcriber_client.transcribe(parsed.media_id)
            logger.info(
                f"Audio transcribed [client={client_id} user={parsed.user_phone}]: "
                f"{len(message_text)} chars"
            )
            return message_text
        except Exception as transcribe_err:
            logger.exception(
                f"Transcription failed [client={client_id} user={parsed.user_phone}]: "
                f"{transcribe_err}"
            )
            try:
                await whatsapp_client.send_message(parsed.user_phone, AUDIO_FALLBACK_MESSAGE)
            except Exception:
                logger.exception(f"Failed to send audio fallback to {parsed.user_phone}")
            return None


async def _wait_for_debounce(context: ConversationContext, client_id: str, user_phone: str) -> None:
    """Poll until the debounce window closes, max 10 seconds."""
    waited = 0.0
    while waited < DEBOUNCE_POLL_MAX:
        await asyncio.sleep(0.2)
        waited += 0.2
        if not context.is_debounce_active(client_id, user_phone):
            break


async def _run_conversation_turn(
    context: ConversationContext, 
    config: ClientConfig, 
    user_phone: str, 
    sheets: Any, 
    whatsapp: Any,
    user_text: str
) -> TurnResult:
    """Load history, run the agent loop, send reply via WhatsApp, and return the result."""
    client_id = str(config.id)
    history: ConversationHistory = context.load_history(client_id, user_phone)

    start_time = time.monotonic()
    reply: str = FALLBACK_MESSAGE
    tool_name: Optional[str] = None
    agent_succeeded = False

    try:
        with logfire.span("agent_run", client_id=client_id, user_phone=user_phone):
            reply, tool_name = await run_agent(
                config=config,
                history=history,
                user_message=user_text,
                sheets=sheets,
                redis=context.redis,
                user_phone=user_phone,
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
                await whatsapp.send_message(user_phone, reply)
            except Exception as send_err:
                logger.exception(
                    f"Failed to send WhatsApp reply to {user_phone}: {send_err}"
                )

    latency_ms = int((time.monotonic() - start_time) * 1000)
    logger.info(
        f"client={client_id} user={user_phone} "
        f"latency={latency_ms}ms "
        f"succeeded={agent_succeeded}"
    )

    # Save to Redis
    try:
        history = context.append_message(
            history, Message(role="user", content=user_text)
        )
        history = context.append_message(
            history, Message(role="assistant", content=reply, tool_name=tool_name)
        )
        context.save_history(client_id, user_phone, history)
    except Exception:
        logger.exception(
            f"Failed to save history [client={client_id} user={user_phone}]"
        )

    messages = [
        Message(role="user", content=user_text),
        Message(role="assistant", content=reply, tool_name=tool_name),
    ]

    return TurnResult(reply=reply, latency_ms=latency_ms, messages=messages, tool_name=tool_name)


# ---------------------------------------------------------------------------
# Background persistence — runs after reply is sent, never raises
# ---------------------------------------------------------------------------

async def _persist(
    conversation_service: Any,
    client_id: str,
    user_phone: str,
    messages: list[Message],
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
    conversation_context: ConversationContext,
    conversation_service: Any,       # ConversationService instance
    whatsapp_client: Any,            # WhatsAppClient instance
    sheets_client: Any,              # SheetsClient instance
    transcriber_client: Any = None,  # TranscriberClient instance (optional)
) -> None:
    """
    Handle a single inbound WhatsApp message end-to-end.
    Never raises — all errors are logged and a Spanish fallback is sent when possible.
    """

    parsed = parse_payload(payload)
    if parsed is None:
        logger.debug("Payload contains no supported message — skipping.")
        return

    with logfire.span("dedup_check", message_id=parsed.message_id):
        if conversation_context.is_duplicate(parsed.message_id):
            logger.info(f"Duplicate message {parsed.message_id} — skipping.")
            return

    client_config = await _resolve_client(parsed.inbound_number, get_client_by_phone)
    if client_config is None:
        return

    client_id = str(client_config.id)

    message_text = await _resolve_text(parsed, client_id, transcriber_client, whatsapp_client)
    if message_text is None:
        return

    conversation_context.push_to_buffer(client_id, parsed.user_phone, parsed.message_id, message_text)

    await _wait_for_debounce(conversation_context, client_id, parsed.user_phone)

    with logfire.span("handle_message", user_phone=parsed.user_phone, client_id=client_id):
        try:
            async with conversation_context.lock(client_id, parsed.user_phone):

                buffered = conversation_context.drain_buffer(client_id, parsed.user_phone)
                if not buffered:
                    logger.debug(
                        f"Buffer already drained by another handler "
                        f"[client={client_id} user={parsed.user_phone}]"
                    )
                    return

                if len(buffered) == 1:
                    combined_text = buffered[0]["text"]
                else:
                    combined_text = "\n".join(m["text"] for m in buffered)
                    logger.info(
                        f"Combined {len(buffered)} buffered messages "
                        f"[client={client_id} user={parsed.user_phone}]"
                    )

                turn_result = await _run_conversation_turn(
                    context=conversation_context,
                    config=client_config,
                    user_phone=parsed.user_phone,
                    sheets=sheets_client,
                    whatsapp=whatsapp_client,
                    user_text=combined_text
                )

                if conversation_service is not None:
                    background_tasks.add_task(
                        _persist,
                        conversation_service,
                        client_id,
                        parsed.user_phone,
                        turn_result.messages,
                        client_config.prompt_version,
                        turn_result.latency_ms,
                    )

        except RuntimeError as lock_err:
            logger.warning(
                f"Lock contention [client={client_id} user={parsed.user_phone}]: {lock_err}"
            )
        except Exception:
            logger.exception(
                f"Unexpected error in handle_message [client={client_id} user={parsed.user_phone}]"
            )