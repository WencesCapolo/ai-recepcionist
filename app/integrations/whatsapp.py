import logging
from functools import lru_cache

import httpx
from app.config import settings

logger = logging.getLogger(__name__)


class WhatsAppClient:
    def __init__(self) -> None:
        self.access_token = settings.whatsapp_access_token
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.base_url = f"https://graph.facebook.com/v19.0/{self.phone_number_id}/messages"

    async def send_message(self, to: str, text: str) -> None:
        # Meta requires E.164 format with + prefix
        if not to.startswith("+"):
            to = f"+{to}"        

        # Argentina-specific: Meta webhooks deliver numbers as +549XXXXXXXXXX
        # but the sending API requires +54XXXXXXXXXX (no 9 after country code)
        if to.startswith("+549"):
            to = "+54" + to[4:]

        if len(text) > 4096:
            logger.warning("Message to %s over 4096 chars, truncating.", to)
            text = text[:4096]

        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.base_url,
                json=payload,
                headers=headers,
            )

            if not response.is_success:
                logger.error(
                    "WhatsApp API error. Status: %s, Body: %s",
                    response.status_code,
                    response.text,
                )
                response.raise_for_status()


@lru_cache
def get_whatsapp_client() -> WhatsAppClient:
    return WhatsAppClient()
