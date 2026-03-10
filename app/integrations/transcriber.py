"""
Transcription client for WhatsApp voice messages.

Flow:
  1. Exchange media_id for a download URL via Meta Graph API.
  2. Download the audio bytes (ogg/opus).
  3. Send to OpenAI Whisper API → transcript string.
"""

import logging
from functools import lru_cache

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class TranscriberClient:
    """Downloads WhatsApp audio and transcribes via OpenAI Whisper."""

    def __init__(self) -> None:
        self.whatsapp_token = settings.whatsapp_access_token
        self.openai_key = settings.openai_api_key

    async def transcribe(self, media_id: str) -> str:
        """
        Given a WhatsApp media_id, return the transcribed text.

        Raises on HTTP or Whisper errors — caller is responsible
        for catching and sending a fallback message.
        """
        meta_headers = {"Authorization": f"Bearer {self.whatsapp_token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. Get media URL from Meta
            meta_resp = await client.get(
                f"https://graph.facebook.com/v19.0/{media_id}",
                headers=meta_headers,
            )
            meta_resp.raise_for_status()
            media_url: str = meta_resp.json()["url"]

            # 2. Download audio bytes
            audio_resp = await client.get(media_url, headers=meta_headers)
            audio_resp.raise_for_status()
            audio_bytes = audio_resp.content

        # 3. Send to Whisper
        async with httpx.AsyncClient(timeout=60.0) as client:
            whisper_resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.openai_key}"},
                data={"model": "whisper-1", "language": "es"},
                files={"file": ("audio.ogg", audio_bytes, "audio/ogg")},
            )
            whisper_resp.raise_for_status()
            transcript: str = whisper_resp.json()["text"]

        logger.info("Transcribed audio %s → %d chars", media_id, len(transcript))
        return transcript


@lru_cache
def get_transcriber_client() -> TranscriberClient:
    return TranscriberClient()
