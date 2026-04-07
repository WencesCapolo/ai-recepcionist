import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.webhook.payload_parser import parse_payload, ParsedMessage
from app.webhook.handler import _resolve_client, _resolve_text

@pytest.fixture
def text_payload():
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"display_phone_number": "123456789"},
                            "messages": [
                                {
                                    "id": "msg1",
                                    "from": "987654321",
                                    "type": "text",
                                    "text": {"body": "hello"}
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def test_parse_payload_text(text_payload):
    parsed = parse_payload(text_payload)
    assert parsed.message_id == "msg1"
    assert parsed.user_phone == "987654321"
    assert parsed.message_text == "hello"
    assert parsed.inbound_number == "123456789"
    assert parsed.media_id is None


@pytest.mark.asyncio
async def test_resolve_client_found():
    client_cfg = MagicMock()
    client_cfg.id = "client1"
    client_cfg.name = "Test Club"
    mock_get_client = AsyncMock(return_value=client_cfg)
    client = await _resolve_client("123456789", mock_get_client)
    mock_get_client.assert_called_once_with("123456789")
    assert client is client_cfg


@pytest.mark.asyncio
async def test_resolve_client_not_found():
    mock_get_client = AsyncMock(return_value=None)
    client = await _resolve_client("123456789", mock_get_client)
    assert client is None


@pytest.mark.asyncio
async def test_resolve_text_with_text():
    parsed = ParsedMessage(
        message_id="msg1",
        user_phone="987",
        message_text="some text",
        inbound_number="123",
        media_id=None
    )
    result = await _resolve_text(parsed, "client1", None, None)
    assert result == "some text"
