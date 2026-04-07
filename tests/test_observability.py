"""
Tests for app/observability.py — WideEvent builder and ENV_CONTEXT.
"""

import logging
import time
from unittest.mock import patch, MagicMock

import pytest

from app.observability import WideEvent, ENV_CONTEXT, logger


# ---------------------------------------------------------------------------
# ENV_CONTEXT
# ---------------------------------------------------------------------------

def test_env_context_has_required_keys():
    required = {"service", "version", "commit_hash", "environment", "region"}
    assert required.issubset(ENV_CONTEXT.keys())


def test_env_context_service_name():
    assert ENV_CONTEXT["service"] == "ai-recepcionist"


def test_env_context_reads_env_vars():
    with patch.dict("os.environ", {"SERVICE_VERSION": "2.0.0", "GIT_COMMIT": "abc123"}):
        # Re-import after patching to verify the reading mechanism
        import importlib
        import app.observability as obs_module
        importlib.reload(obs_module)
        assert obs_module.ENV_CONTEXT["version"] == "2.0.0"
        assert obs_module.ENV_CONTEXT["commit_hash"] == "abc123"
        # Reload back to original state
        importlib.reload(obs_module)


# ---------------------------------------------------------------------------
# WideEvent — initialization
# ---------------------------------------------------------------------------

def test_wide_event_initial_fields():
    we = WideEvent(message_id="wamid.test123")
    assert we._data["message_id"] == "wamid.test123"
    assert we._data["outcome"] == "unknown"
    assert we._data["tools_used"] == []
    assert we._data["iterations"] == 0
    assert we._data["reply_length"] == 0
    assert we._data["error"] is None
    assert isinstance(we._data["latency_breakdown"], dict)
    assert "timestamp" in we._data


# ---------------------------------------------------------------------------
# WideEvent — set_client
# ---------------------------------------------------------------------------

def test_set_client():
    we = WideEvent(message_id="msg1")
    we.set_client("client-uuid", "Padel Club", "+54911123")
    assert we._data["client_id"] == "client-uuid"
    assert we._data["client_name"] == "Padel Club"
    assert we._data["inbound_number"] == "+54911123"


# ---------------------------------------------------------------------------
# WideEvent — set_user (privacy: sha256 hash)
# ---------------------------------------------------------------------------

def test_set_user_stores_hash_not_raw_phone():
    we = WideEvent(message_id="msg1")
    phone = "+5491112345678"
    we.set_user(phone)
    assert "user_phone_hash" in we._data
    assert we._data["user_phone_hash"] != phone


def test_set_user_hash_is_8_chars():
    we = WideEvent(message_id="msg1")
    we.set_user("+5491112345678")
    assert len(we._data["user_phone_hash"]) == 8


def test_set_user_hash_is_deterministic():
    phone = "+5491112345678"
    we1 = WideEvent(message_id="msg1")
    we2 = WideEvent(message_id="msg2")
    we1.set_user(phone)
    we2.set_user(phone)
    assert we1._data["user_phone_hash"] == we2._data["user_phone_hash"]


def test_set_user_different_phones_produce_different_hashes():
    we1 = WideEvent(message_id="msg1")
    we2 = WideEvent(message_id="msg2")
    we1.set_user("+5491112345678")
    we2.set_user("+5491198765432")
    assert we1._data["user_phone_hash"] != we2._data["user_phone_hash"]


# ---------------------------------------------------------------------------
# WideEvent — set_outcome
# ---------------------------------------------------------------------------

def test_set_outcome_success():
    we = WideEvent(message_id="msg1")
    we.set_outcome("success")
    assert we._data["outcome"] == "success"
    assert we._data["error"] is None


def test_set_outcome_error_with_exception():
    we = WideEvent(message_id="msg1")
    exc = ValueError("something went wrong")
    we.set_outcome("error", error=exc)
    assert we._data["outcome"] == "error"
    assert we._data["error"]["type"] == "ValueError"
    assert we._data["error"]["message"] == "something went wrong"


def test_set_outcome_error_without_exception():
    we = WideEvent(message_id="msg1")
    we.set_outcome("skipped_duplicate")
    assert we._data["outcome"] == "skipped_duplicate"
    assert we._data["error"] is None


# ---------------------------------------------------------------------------
# WideEvent — set_agent_result
# ---------------------------------------------------------------------------

def test_set_agent_result():
    we = WideEvent(message_id="msg1")
    we.set_agent_result(iterations=3, reply="Hola! Cómo puedo ayudarte?", tools_used=["get_products"])
    assert we._data["iterations"] == 3
    assert we._data["reply_length"] == len("Hola! Cómo puedo ayudarte?")
    assert we._data["tools_used"] == ["get_products"]


def test_set_agent_result_empty_tools():
    we = WideEvent(message_id="msg1")
    we.set_agent_result(iterations=1, reply="Hola!", tools_used=[])
    assert we._data["tools_used"] == []
    assert we._data["iterations"] == 1


# ---------------------------------------------------------------------------
# WideEvent — set_latency
# ---------------------------------------------------------------------------

def test_set_latency():
    we = WideEvent(message_id="msg1")
    we.set_latency("agent_run_ms", 1234.5)
    we.set_latency("whatsapp_send_ms", 200.0)
    assert we._data["latency_breakdown"]["agent_run_ms"] == 1234.5
    assert we._data["latency_breakdown"]["whatsapp_send_ms"] == 200.0


# ---------------------------------------------------------------------------
# WideEvent — emit
# ---------------------------------------------------------------------------

def test_emit_calls_logger_info_once():
    we = WideEvent(message_id="msg1")
    we.set_outcome("success")

    with patch.object(logger, "info") as mock_info:
        we.emit()
        mock_info.assert_called_once()


def test_emit_includes_env_context():
    we = WideEvent(message_id="msg1")
    we.set_outcome("success")

    emitted: dict = {}

    def capture(data):
        emitted.update(data)

    with patch.object(logger, "info", side_effect=capture):
        we.emit()

    assert emitted["service"] == "ai-recepcionist"
    assert "version" in emitted
    assert "commit_hash" in emitted
    assert "environment" in emitted
    assert "region" in emitted


def test_emit_includes_message_and_outcome():
    we = WideEvent(message_id="wamid.xyz")
    we.set_outcome("success")

    emitted: dict = {}

    def capture(data):
        emitted.update(data)

    with patch.object(logger, "info", side_effect=capture):
        we.emit()

    assert emitted["message_id"] == "wamid.xyz"
    assert emitted["outcome"] == "success"


def test_emit_includes_positive_latency_ms():
    we = WideEvent(message_id="msg1")
    we.set_outcome("success")

    emitted: dict = {}

    def capture(data):
        emitted.update(data)

    with patch.object(logger, "info", side_effect=capture):
        we.emit()

    assert "latency_ms" in emitted
    assert emitted["latency_ms"] >= 0


def test_emit_latency_increases_over_time():
    we = WideEvent(message_id="msg1")
    time.sleep(0.01)  # 10ms minimum
    we.set_outcome("success")

    emitted: dict = {}

    def capture(data):
        emitted.update(data)

    with patch.object(logger, "info", side_effect=capture):
        we.emit()

    assert emitted["latency_ms"] >= 10


def test_emit_full_wide_event_shape():
    we = WideEvent(message_id="wamid.full")
    we.set_client("uuid-client", "Club Test", "+54911000")
    we.set_user("+5491187654321")
    we.set_agent_result(iterations=2, reply="Tu turno fue reservado.", tools_used=["book_appointment"])
    we.set_latency("agent_run_ms", 1500.0)
    we.set_latency("whatsapp_send_ms", 210.0)
    we.set_outcome("success")

    emitted: dict = {}

    def capture(data):
        emitted.update(data)

    with patch.object(logger, "info", side_effect=capture):
        we.emit()

    assert emitted["service"] == "ai-recepcionist"
    assert emitted["message_id"] == "wamid.full"
    assert emitted["client_id"] == "uuid-client"
    assert emitted["client_name"] == "Club Test"
    assert emitted["inbound_number"] == "+54911000"
    assert len(emitted["user_phone_hash"]) == 8
    assert emitted["outcome"] == "success"
    assert emitted["tools_used"] == ["book_appointment"]
    assert emitted["iterations"] == 2
    assert emitted["reply_length"] == len("Tu turno fue reservado.")
    assert emitted["latency_breakdown"]["agent_run_ms"] == 1500.0
    assert emitted["latency_breakdown"]["whatsapp_send_ms"] == 210.0
    assert emitted["latency_ms"] >= 0
    assert emitted["error"] is None


# ---------------------------------------------------------------------------
# WideEvent — logger identity
# ---------------------------------------------------------------------------

def test_logger_name():
    assert logger.name == "ai-recepcionist"
