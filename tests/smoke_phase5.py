import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.main import app, create_app
from app.config import settings
from app.dependencies import (
    get_client_service,
    get_conversation_context,
    get_conversation_service,
    get_whatsapp_client,
    get_sheets_client,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(message_id: str = "msg-abc-123", text: str = "Hola") -> dict:
    """Minimal valid Meta webhook payload with a text message."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry-123",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "+5493511234567",
                                "phone_number_id": "123456",
                            },
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": "+5493519999999",
                                    "type": "text",
                                    "text": {"body": text},
                                    "timestamp": "1700000000",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _override_dependencies(app, *, context_mock=None, whatsapp_mock=None):
    """
    Override all infrastructure dependencies with lightweight mocks.
    Returns the mocks so tests can assert on them.
    """
    if context_mock is None:
        context_mock = MagicMock()
        context_mock.is_duplicate.return_value = False
        # lock() must work as an async context manager
        lock_cm = MagicMock()
        lock_cm.__aenter__ = AsyncMock(return_value=None)
        lock_cm.__aexit__ = AsyncMock(return_value=False)
        context_mock.lock.return_value = lock_cm
        context_mock.load_history.return_value = MagicMock(messages=[])
        context_mock.save_history.return_value = None

    if whatsapp_mock is None:
        whatsapp_mock = MagicMock()
        whatsapp_mock.send_message = AsyncMock()

    client_service_mock = MagicMock()
    client_service_mock.get_by_phone = AsyncMock(return_value=None)  # no client → silent return

    conversation_service_mock = MagicMock()
    conversation_service_mock.upsert_conversation = AsyncMock(return_value="conv-uuid-123")
    conversation_service_mock.log_messages = AsyncMock()

    sheets_mock = MagicMock()

    app.dependency_overrides[get_client_service] = lambda: client_service_mock
    app.dependency_overrides[get_conversation_context] = lambda: context_mock
    app.dependency_overrides[get_conversation_service] = lambda: conversation_service_mock
    app.dependency_overrides[get_whatsapp_client] = lambda: whatsapp_mock
    app.dependency_overrides[get_sheets_client] = lambda: sheets_mock

    return {
        "client_service": client_service_mock,
        "context": context_mock,
        "conversation_service": conversation_service_mock,
        "whatsapp": whatsapp_mock,
        "sheets": sheets_mock,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """TestClient with a fresh app instance per test."""
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_response_shape(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert "environment" in body

    def test_environment_matches_settings(self, client):
        body = client.get("/health").json()
        assert body["environment"] == settings.environment


# ---------------------------------------------------------------------------
# GET /webhook  (Meta verification handshake)
# ---------------------------------------------------------------------------

class TestWebhookVerification:
    def test_correct_token_returns_challenge(self, client):
        from pydantic import SecretStr
        with patch.object(settings, "whatsapp_verify_token", SecretStr("test_token_123")):
            response = client.get(
                "/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "test_token_123",
                    "hub.challenge": "ch_123456789",
                },
            )
        assert response.status_code == 200
        assert response.text == "ch_123456789"

    def test_wrong_token_returns_403(self, client):
        from pydantic import SecretStr
        with patch.object(settings, "whatsapp_verify_token", SecretStr("test_token_123")):
            response = client.get(
                "/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong_token",
                    "hub.challenge": "ch_123456789",
                },
            )
        assert response.status_code == 403

    def test_wrong_mode_returns_403(self, client):
        with patch.object(settings, "whatsapp_verify_token", "test_token_123"):
            response = client.get(
                "/webhook",
                params={
                    "hub.mode": "unsubscribe",
                    "hub.verify_token": "test_token_123",
                    "hub.challenge": "ch_123456789",
                },
            )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /webhook  (incoming messages)
# ---------------------------------------------------------------------------

class TestWebhookPost:
    def test_always_returns_200(self, client):
        """Meta requires HTTP 200 within 5s — non-negotiable."""
        _override_dependencies(app)
        response = client.post("/webhook", json=_make_payload())
        assert response.status_code == 200

    def test_returns_200_even_on_agent_crash(self, client):
        """
        If the agent explodes, the endpoint must still return 200
        (Meta will retry on non-200, causing duplicate messages).
        """
        mocks = _override_dependencies(app)

        # Give the payload a valid client so the agent path is reached
        from app.clients.models import ClientConfig
        import uuid

        fake_config = ClientConfig(
            id=uuid.uuid4(),
            name="Ferretería Stainless",
            whatsapp_number="+5493511234567",
            system_prompt="Sos el asistente de Ferretería Stainless.\nHorario: lunes a viernes 8-18.",
            tools_enabled=[],
            prompt_version=1,
            active=True,
        )
        mocks["client_service"].get_client_by_phone = AsyncMock(return_value=fake_config)

        with patch("app.webhook.handler.run_agent", side_effect=Exception("LLM exploded")):
            response = client.post("/webhook", json=_make_payload())

        assert response.status_code == 200

    def test_fallback_message_sent_on_agent_crash(self, client):
        """
        When the agent crashes, a Spanish fallback message must be
        sent to the user via whatsapp_client.send_message.
        """
        from app.clients.models import ClientConfig
        import uuid

        whatsapp_mock = MagicMock()
        whatsapp_mock.send_message = AsyncMock()

        mocks = _override_dependencies(app, whatsapp_mock=whatsapp_mock)

        fake_config = ClientConfig(
            id=uuid.uuid4(),
            name="Ferretería Stainless",
            whatsapp_number="+5493511234567",
            system_prompt="Sos el asistente de Ferretería Stainless.\nHorario: lunes a viernes 8-18.",
            tools_enabled=[],
            prompt_version=1,
            active=True,
        )
        mocks["client_service"].get_client_by_phone = AsyncMock(return_value=fake_config)

        with patch("app.webhook.handler.run_agent", side_effect=Exception("LLM exploded")):
            client.post("/webhook", json=_make_payload(message_id="crash-test-msg"))

        whatsapp_mock.send_message.assert_called_once()
        _, call_kwargs = whatsapp_mock.send_message.call_args
        sent_text = call_kwargs.get("text") or whatsapp_mock.send_message.call_args[0][1]

        # Must be in Spanish and non-empty
        assert len(sent_text) > 0
        assert any(
            word in sent_text.lower()
            for word in ["lo siento", "problema", "intentá", "disculp"]
        ), f"Fallback message doesn't look like Spanish: '{sent_text}'"

    def test_duplicate_message_ignored(self, client):
        """
        Sending the same message_id twice must result in handle_message
        being short-circuited on the second call (dedup).
        """
        from app.clients.models import ClientConfig
        import uuid

        whatsapp_mock = MagicMock()
        whatsapp_mock.send_message = AsyncMock()

        fake_config = ClientConfig(
            id=uuid.uuid4(),
            name="Ferretería Stainless",
            whatsapp_number="+5493511234567",
            system_prompt="Test.\nHorario: lunes a viernes 8-18.",
            tools_enabled=[],
            prompt_version=1,
            active=True,
        )

        # First call: not duplicate. Second call: duplicate.
        context_mock = MagicMock()
        context_mock.is_duplicate.side_effect = [False, True]
        lock_cm = MagicMock()
        lock_cm.__aenter__ = AsyncMock(return_value=None)
        lock_cm.__aexit__ = AsyncMock(return_value=False)
        context_mock.lock.return_value = lock_cm
        context_mock.load_history.return_value = MagicMock(messages=[])
        context_mock.save_history.return_value = None

        mocks = _override_dependencies(app, context_mock=context_mock, whatsapp_mock=whatsapp_mock)
        mocks["client_service"].get_client_by_phone = AsyncMock(return_value=fake_config)

        payload = _make_payload(message_id="dup-msg-999")

        with patch("app.webhook.handler.run_agent", new_callable=AsyncMock) as mock_agent:
            mock_agent.return_value = ("Hola!", [], 1)
            client.post("/webhook", json=payload)  # first — should process
            client.post("/webhook", json=payload)  # second — should be dropped

        # Agent must only have been called once
        assert mock_agent.call_count == 1

    def test_payload_without_text_message_ignored(self, client):
        """
        Payloads that carry no text message (e.g. status updates,
        read receipts) must be silently dropped — no agent call, 200 returned.
        """
        _override_dependencies(app)

        status_payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "entry-123",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "+5493511234567",
                                    "phone_number_id": "123456",
                                },
                                "statuses": [
                                    {
                                        "id": "msg-abc-123",
                                        "status": "delivered",
                                        "timestamp": "1700000000",
                                        "recipient_id": "+5493519999999",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        with patch("app.webhook.handler.run_agent", new_callable=AsyncMock) as mock_agent:
            response = client.post("/webhook", json=status_payload)

        assert response.status_code == 200
        mock_agent.assert_not_called()

    def test_unknown_client_silently_ignored(self, client):
        """
        If the inbound number doesn't match any active client in Supabase,
        the message is dropped — no agent call, 200 returned.
        """
        mocks = _override_dependencies(app)
        mocks["client_service"].get_by_phone = AsyncMock(return_value=None)

        with patch("app.webhook.handler.run_agent", new_callable=AsyncMock) as mock_agent:
            response = client.post("/webhook", json=_make_payload())

        assert response.status_code == 200
        mock_agent.assert_not_called()