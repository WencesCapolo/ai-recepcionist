"""
Tests for handler.py observability integration — WideEvent threading and span structure.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from fastapi import BackgroundTasks

from app.webhook.handler import handle_message, TurnResult, _resolve_client, _resolve_text
from app.webhook.payload_parser import ParsedMessage
from app.observability import WideEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_text_payload(message_id="msg1", user_phone="5491187654321", body="hola"):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"display_phone_number": "15550001111"},
                    "messages": [{
                        "id": message_id,
                        "from": user_phone,
                        "type": "text",
                        "text": {"body": body}
                    }]
                }
            }]
        }]
    }


def make_client_config(client_id="uuid-abc", name="Test Club", prompt_version=1):
    cfg = MagicMock()
    cfg.id = client_id
    cfg.name = name
    cfg.prompt_version = prompt_version
    return cfg


# ---------------------------------------------------------------------------
# TurnResult dataclass
# ---------------------------------------------------------------------------

def test_turn_result_has_tools_used_and_iterations():
    tr = TurnResult(
        reply="hola",
        latency_ms=100,
        agent_ms=80,
        send_ms=20,
        messages=[],
        tools_used=["get_products"],
        iterations=2,
        agent_succeeded=True,
    )
    assert tr.tools_used == ["get_products"]
    assert tr.iterations == 2
    assert tr.agent_succeeded is True
    assert tr.agent_ms == 80
    assert tr.send_ms == 20


def test_turn_result_defaults():
    tr = TurnResult(reply="x", latency_ms=0, agent_ms=0, send_ms=0, messages=[])
    assert tr.tools_used == []
    assert tr.iterations == 0
    assert tr.agent_succeeded is True


# ---------------------------------------------------------------------------
# _resolve_client — enriches span with client info
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_client_found_sets_span_attributes():
    client_cfg = make_client_config()
    mock_get = AsyncMock(return_value=client_cfg)

    mock_span = MagicMock()
    mock_span.__enter__ = MagicMock(return_value=mock_span)
    mock_span.__exit__ = MagicMock(return_value=False)

    with patch("logfire.span", return_value=mock_span):
        result = await _resolve_client("15550001111", mock_get)

    assert result is client_cfg
    calls = [c.args for c in mock_span.set_attribute.call_args_list]
    assert ("client_id", "uuid-abc") in calls
    assert ("client_name", "Test Club") in calls


@pytest.mark.asyncio
async def test_resolve_client_not_found_returns_none():
    mock_get = AsyncMock(return_value=None)

    mock_span = MagicMock()
    mock_span.__enter__ = MagicMock(return_value=mock_span)
    mock_span.__exit__ = MagicMock(return_value=False)

    with patch("logfire.span", return_value=mock_span):
        result = await _resolve_client("15550001111", mock_get)

    assert result is None
    mock_span.set_attribute.assert_not_called()


# ---------------------------------------------------------------------------
# handle_message — WideEvent emits in all paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_message_emits_wide_event_on_duplicate():
    """Duplicate message: WideEvent should emit with outcome=skipped_duplicate."""
    payload = make_text_payload()
    bg = BackgroundTasks()

    ctx = MagicMock()
    ctx.is_duplicate.return_value = True

    emitted = {}

    with patch("app.webhook.handler.WideEvent") as MockWE, \
         patch("logfire.span") as mock_span:
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_we = MagicMock()
        MockWE.return_value = mock_we

        await handle_message(
            payload=payload,
            background_tasks=bg,
            get_client_by_phone=AsyncMock(),
            conversation_context=ctx,
            conversation_service=None,
            whatsapp_client=MagicMock(),
            sheets_client=MagicMock(),
        )

    mock_we.set_outcome.assert_called_with("skipped_duplicate")
    mock_we.emit.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_emits_wide_event_on_no_client():
    """No client config: WideEvent should emit with outcome=skipped_no_client."""
    payload = make_text_payload()
    bg = BackgroundTasks()

    ctx = MagicMock()
    ctx.is_duplicate.return_value = False

    with patch("app.webhook.handler.WideEvent") as MockWE, \
         patch("app.webhook.handler._resolve_client", new=AsyncMock(return_value=None)), \
         patch("logfire.span") as mock_span:
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_we = MagicMock()
        MockWE.return_value = mock_we

        await handle_message(
            payload=payload,
            background_tasks=bg,
            get_client_by_phone=AsyncMock(return_value=None),
            conversation_context=ctx,
            conversation_service=None,
            whatsapp_client=MagicMock(),
            sheets_client=MagicMock(),
        )

    mock_we.set_outcome.assert_called_with("skipped_no_client")
    mock_we.emit.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_wide_event_emit_always_fires_on_success():
    """Full success path: WideEvent emits once with outcome=success."""
    payload = make_text_payload()
    bg = BackgroundTasks()
    client_cfg = make_client_config()

    ctx = MagicMock()
    ctx.is_duplicate.return_value = False
    ctx.is_debounce_active.return_value = False
    ctx.drain_buffer.return_value = [{"id": "msg1", "text": "hola"}]
    ctx.load_history.return_value = MagicMock(messages=[])
    ctx.lock.return_value.__aenter__ = AsyncMock(return_value=None)
    ctx.lock.return_value.__aexit__ = AsyncMock(return_value=False)

    turn_result = TurnResult(
        reply="Hola, cómo te puedo ayudar?",
        latency_ms=300,
        agent_ms=250,
        send_ms=50,
        messages=[],
        tools_used=["get_products"],
        iterations=2,
        agent_succeeded=True,
    )

    with patch("app.webhook.handler.WideEvent") as MockWE, \
         patch("app.webhook.handler._resolve_client", new=AsyncMock(return_value=client_cfg)), \
         patch("app.webhook.handler._resolve_text", new=AsyncMock(return_value="hola")), \
         patch("app.webhook.handler._run_conversation_turn", new=AsyncMock(return_value=turn_result)), \
         patch("app.webhook.handler._wait_for_debounce", new=AsyncMock()), \
         patch("logfire.span") as mock_span:
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_we = MagicMock()
        MockWE.return_value = mock_we

        await handle_message(
            payload=payload,
            background_tasks=bg,
            get_client_by_phone=AsyncMock(return_value=client_cfg),
            conversation_context=ctx,
            conversation_service=None,
            whatsapp_client=MagicMock(),
            sheets_client=MagicMock(),
        )

    mock_we.emit.assert_called_once()
    mock_we.set_outcome.assert_called_with("success")
    mock_we.set_agent_result.assert_called_once_with(
        iterations=2,
        reply="Hola, cómo te puedo ayudar?",
        tools_used=["get_products"],
    )
    mock_we.set_latency.assert_any_call("agent_run_ms", 250)
    mock_we.set_latency.assert_any_call("whatsapp_send_ms", 50)


@pytest.mark.asyncio
async def test_handle_message_sets_client_and_user_on_wide_event():
    """After client resolution, WideEvent.set_client and set_user are called."""
    payload = make_text_payload(user_phone="5491199998888")
    bg = BackgroundTasks()
    client_cfg = make_client_config(client_id="cid-999", name="My Club")

    ctx = MagicMock()
    ctx.is_duplicate.return_value = False
    ctx.is_debounce_active.return_value = False
    ctx.drain_buffer.return_value = [{"id": "msg1", "text": "hola"}]
    ctx.load_history.return_value = MagicMock(messages=[])
    ctx.lock.return_value.__aenter__ = AsyncMock(return_value=None)
    ctx.lock.return_value.__aexit__ = AsyncMock(return_value=False)

    turn_result = TurnResult(
        reply="OK", latency_ms=100, agent_ms=80, send_ms=20,
        messages=[], tools_used=[], iterations=1, agent_succeeded=True,
    )

    with patch("app.webhook.handler.WideEvent") as MockWE, \
         patch("app.webhook.handler._resolve_client", new=AsyncMock(return_value=client_cfg)), \
         patch("app.webhook.handler._resolve_text", new=AsyncMock(return_value="hola")), \
         patch("app.webhook.handler._run_conversation_turn", new=AsyncMock(return_value=turn_result)), \
         patch("app.webhook.handler._wait_for_debounce", new=AsyncMock()), \
         patch("logfire.span") as mock_span:
        mock_span.return_value.__enter__ = MagicMock(return_value=None)
        mock_span.return_value.__exit__ = MagicMock(return_value=False)

        mock_we = MagicMock()
        MockWE.return_value = mock_we

        await handle_message(
            payload=payload,
            background_tasks=bg,
            get_client_by_phone=AsyncMock(return_value=client_cfg),
            conversation_context=ctx,
            conversation_service=None,
            whatsapp_client=MagicMock(),
            sheets_client=MagicMock(),
        )

    mock_we.set_client.assert_called_once_with("cid-999", "My Club", "15550001111")
    mock_we.set_user.assert_called_once_with("5491199998888")


@pytest.mark.asyncio
async def test_handle_message_no_emit_when_payload_is_invalid():
    """parse_payload returning None → no WideEvent created, no emit."""
    bg = BackgroundTasks()

    with patch("app.webhook.handler.parse_payload", return_value=None), \
         patch("app.webhook.handler.WideEvent") as MockWE:
        await handle_message(
            payload={},
            background_tasks=bg,
            get_client_by_phone=AsyncMock(),
            conversation_context=MagicMock(),
            conversation_service=None,
            whatsapp_client=MagicMock(),
            sheets_client=MagicMock(),
        )

    MockWE.assert_not_called()
