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
import time
import logfire
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks

from app.agent.graph import run_agent
from app.clients.models import ClientConfig
from app.context.redis import ConversationContext
from app.conversations.models import ConversationHistory, Message
from app.observability import WideEvent, logger
from app.webhook.payload_parser import parse_payload, ParsedMessage

FALLBACK_MESSAGE = (
    "Disculpa, tuve un problema con tu mensaje. "
    "Por favor mandame de nuevo en unos minutos, gracias!"
)

AUDIO_FALLBACK_MESSAGE = (
    "Disculpa, no pude escuchar el audio. Podrías escribirme lo que necesitás?"
)

# Maximum time (seconds) to wait for the debounce window to close.
DEBOUNCE_POLL_MAX = 10


@dataclass
class TurnResult:
    reply: str
    latency_ms: int
    agent_ms: int
    send_ms: int
    messages: list[Message]
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    agent_succeeded: bool = True


# ---------------------------------------------------------------------------
# Extracted single-responsibility steps
# ---------------------------------------------------------------------------

async def _resolve_client(inbound_number: str, get_client_by_phone: Callable) -> Optional[ClientConfig]:
    """Retrieve client config by inbound phone number."""
    with logfire.span("client_lookup", inbound_number=inbound_number) as span:
        client_config = await get_client_by_phone(inbound_number)
        if client_config is None:
            logger.warning(f"No active client for inbound number {inbound_number} — skipping.")
        else:
            span.set_attribute("client_id", str(client_config.id))
            span.set_attribute("client_name", client_config.name)
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

    with logfire.span("audio_transcription", media_id=parsed.media_id, client_id=client_id) as span:
        try:
            message_text = await transcriber_client.transcribe(parsed.media_id)
            span.set_attribute("transcribed_length", len(message_text))
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

    reply: str = FALLBACK_MESSAGE
    tools_used: list[str] = []
    iterations: int = 0
    agent_succeeded = False
    agent_ms = 0
    send_ms = 0

    agent_start = time.monotonic()
    try:
        with logfire.span("agent_run", client_id=client_id) as span:
            reply, tools_used, iterations = await run_agent(
                config=config,
                history=history,
                user_message=user_text,
                sheets=sheets,
                redis=context.redis,
                user_phone=user_phone,
            )
            span.set_attribute("iterations", iterations)
            span.set_attribute("tools_used", tools_used)
            span.set_attribute("reply_length", len(reply))
        agent_succeeded = True

    except Exception as agent_err:
        logger.exception(
            f"Agent error [client={client_id} user={user_phone}]: {agent_err}"
        )
        reply = FALLBACK_MESSAGE

    finally:
        agent_ms = int((time.monotonic() - agent_start) * 1000)

    send_start = time.monotonic()
    with logfire.span("whatsapp_send", client_id=client_id, reply_length=len(reply)) as span:
        try:
            await whatsapp.send_message(user_phone, reply)
        except Exception as send_err:
            logger.exception(
                f"Failed to send WhatsApp reply to {user_phone}: {send_err}"
            )
    send_ms = int((time.monotonic() - send_start) * 1000)

    tool_name = ",".join(tools_used) if tools_used else None

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

    latency_ms = agent_ms + send_ms
    return TurnResult(
        reply=reply,
        latency_ms=latency_ms,
        agent_ms=agent_ms,
        send_ms=send_ms,
        messages=messages,
        tools_used=tools_used,
        iterations=iterations,
        agent_succeeded=agent_succeeded,
    )


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
        with logfire.span("db.persist_conversation", client_id=client_id) as span:
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
            span.set_attribute("messages_logged", len(messages))
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

    wide_event = WideEvent(message_id=parsed.message_id)

    with logfire.span("handle_message", message_id=parsed.message_id):
        try:
            # 1. Dedup
            with logfire.span("dedup_check", message_id=parsed.message_id):
                if conversation_context.is_duplicate(parsed.message_id):
                    wide_event.set_outcome("skipped_duplicate")
                    return

            # 2. Client lookup
            client_config = await _resolve_client(parsed.inbound_number, get_client_by_phone)
            if client_config is None:
                wide_event.set_outcome("skipped_no_client")
                return

            client_id = str(client_config.id)
            wide_event.set_client(client_id, client_config.name, parsed.inbound_number)
            wide_event.set_user(parsed.user_phone)

            # 3. Text resolution (audio → transcription if needed)
            message_text = await _resolve_text(parsed, client_id, transcriber_client, whatsapp_client)
            if message_text is None:
                wide_event.set_outcome("skipped_no_text")
                return

            # 4. Buffer + debounce
            conversation_context.push_to_buffer(client_id, parsed.user_phone, parsed.message_id, message_text)
            await _wait_for_debounce(conversation_context, client_id, parsed.user_phone)

            # 5. Lock, drain, run
            try:
                async with conversation_context.lock(client_id, parsed.user_phone):
                    buffered = conversation_context.drain_buffer(client_id, parsed.user_phone)
                    if not buffered:
                        logger.debug(
                            f"Buffer already drained by another handler "
                            f"[client={client_id} user={parsed.user_phone}]"
                        )
                        wide_event.set_outcome("skipped_buffer_drained")
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

                    wide_event.set_agent_result(
                        iterations=turn_result.iterations,
                        reply=turn_result.reply,
                        tools_used=turn_result.tools_used,
                    )
                    wide_event.set_latency("agent_run_ms", turn_result.agent_ms)
                    wide_event.set_latency("whatsapp_send_ms", turn_result.send_ms)

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

                    outcome = "success" if turn_result.agent_succeeded else "agent_error"
                    wide_event.set_outcome(outcome)

            except RuntimeError as lock_err:
                logger.warning(
                    f"Lock contention [client={client_id} user={parsed.user_phone}]: {lock_err}"
                )
                wide_event.set_outcome("error_lock_timeout", lock_err)
            except Exception as e:
                logger.exception(
                    f"Unexpected error in handle_message [client={client_id} user={parsed.user_phone}]"
                )
                wide_event.set_outcome("error", e)

        except Exception as e:
            logger.exception("Unhandled error in handle_message outer block")
            wide_event.set_outcome("error", e)
        finally:
            wide_event.emit()
