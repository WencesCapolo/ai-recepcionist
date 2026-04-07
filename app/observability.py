"""
app/observability.py

Single module owning all observability infrastructure for ai-recepcionist.

Provides:
  - ENV_CONTEXT: static deployment metadata captured once at startup
  - WideEvent: builder for the canonical log line per WhatsApp message
  - logger: single structured JSON logger used across the app
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Deployment context — captured once at startup
# ---------------------------------------------------------------------------

ENV_CONTEXT: dict = {
    "service": "ai-recepcionist",
    "version": os.getenv("SERVICE_VERSION", "unknown"),
    "commit_hash": os.getenv("GIT_COMMIT", "unknown"),
    "environment": os.getenv("ENVIRONMENT", "development"),
    "region": os.getenv("RAILWAY_REGION", "unknown"),
}

# ---------------------------------------------------------------------------
# Single structured logger — import this everywhere instead of getLogger
# ---------------------------------------------------------------------------

logger = logging.getLogger("ai-recepcionist")


# ---------------------------------------------------------------------------
# Wide Event — one context-rich log line per WhatsApp message
# ---------------------------------------------------------------------------

class WideEvent:
    """Builder for the canonical log line emitted once per WhatsApp message.

    Usage:
        wide_event = WideEvent(message_id="wamid.xxx")
        wide_event.set_client(client_id, client_name, inbound_number)
        wide_event.set_user(user_phone)
        # ... each pipeline step enriches the event ...
        wide_event.set_outcome("success")
        wide_event.emit()  # called in finally block
    """

    def __init__(self, message_id: str) -> None:
        self._start = time.monotonic()
        self._data: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_id": message_id,
            "outcome": "unknown",
            "tools_used": [],
            "iterations": 0,
            "reply_length": 0,
            "error": None,
            "latency_breakdown": {},
        }

    def set_client(self, client_id: str, client_name: str, inbound_number: str) -> None:
        self._data["client_id"] = client_id
        self._data["client_name"] = client_name
        self._data["inbound_number"] = inbound_number

    def set_user(self, user_phone: str) -> None:
        self._data["user_phone_hash"] = hashlib.sha256(user_phone.encode()).hexdigest()[:8]

    def set_outcome(self, outcome: str, error: Optional[Exception] = None) -> None:
        self._data["outcome"] = outcome
        if error is not None:
            self._data["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }

    def set_agent_result(self, iterations: int, reply: str, tools_used: list[str]) -> None:
        self._data["iterations"] = iterations
        self._data["reply_length"] = len(reply)
        self._data["tools_used"] = tools_used

    def set_latency(self, key: str, ms: float) -> None:
        self._data["latency_breakdown"][key] = round(ms, 1)

    def emit(self) -> None:
        """Emit the canonical log line. Call exactly once, in a finally block."""
        self._data["latency_ms"] = int((time.monotonic() - self._start) * 1000)
        logger.info({**ENV_CONTEXT, **self._data})
