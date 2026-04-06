from dataclasses import dataclass
from typing import Optional

@dataclass
class ParsedMessage:
    message_id: str
    user_phone: str
    message_text: Optional[str]
    inbound_number: str
    media_id: Optional[str]

def parse_payload(payload: dict) -> Optional[ParsedMessage]:
    """
    Parse a Meta Cloud API webhook payload.
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
            if msg.get("context"):
                message_text = f"[respondiendo a un mensaje anterior] {message_text}"

            return ParsedMessage(message_id, user_phone, message_text, inbound_number, None)

        if msg_type == "audio":
            media_id: str = msg["audio"]["id"]
            return ParsedMessage(message_id, user_phone, None, inbound_number, media_id)

        return None

    except (KeyError, IndexError, TypeError):
        return None
