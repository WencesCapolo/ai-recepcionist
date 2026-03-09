# tests/smoke_phase4.py
"""
Smoke test for app/agent/graph.py — the full LangGraph agent loop.

Requirements:
    - TEST_SHEET_ID in .env  (the Ferretería Stainless Google Sheet)
    - OPENAI_API_KEY in .env
    - GOOGLE_SERVICE_ACCOUNT_JSON in .env (base64)

Run:
    python -m tests.smoke_phase4
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from app.agent.graph import run_agent
from app.clients.models import ClientConfig
from app.conversations.models import ConversationHistory
from app.integrations.sheets import SheetsClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SHEET_ID = os.getenv("TEST_SHEET_ID", "")
CLIENT_ID = str(uuid.uuid4())


def make_test_config() -> ClientConfig:
    return ClientConfig(
        id=uuid.UUID(CLIENT_ID),
        name="Ferretería Stainless",
        whatsapp_number="+5493511234567",
        system_prompt=(
            "Sos el asistente de Ferretería Stainless.\n"
            "Horario: lunes a viernes 8:00–18:00, sábados 8:00–13:00, domingos cerrado."
        ),
        tools_enabled=["get_price", "get_stock", "get_all_products", "get_hours"],
        sheet_id=SHEET_ID,
        prompt_version=1,
        active=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_price_query():
    print("\n--- Test 1: price query ---")
    config = make_test_config()
    history = ConversationHistory()
    sheets = SheetsClient()

    reply = await run_agent(config, history, "cuánto sale el tornillo 6x50?", sheets)

    print(f"Reply: {reply}")
    assert reply, "❌ Reply is empty"
    assert "$" in reply, f"❌ Expected '$' in reply, got: {reply}"
    print("✅ Price reply contains '$'")


async def test_hours_query():
    print("\n--- Test 2: hours query ---")
    config = make_test_config()
    history = ConversationHistory()
    sheets = SheetsClient()

    reply = await run_agent(config, history, "están abiertos el sábado?", sheets)

    print(f"Reply: {reply}")
    assert reply, "❌ Reply is empty"
    assert "sábado" in reply.lower() or "sabado" in reply.lower(), (
        f"❌ Expected 'sábado' in reply, got: {reply}"
    )
    print("✅ Hours reply mentions 'sábado'")


async def run_all():
    print("=== Phase 4 Smoke Test — Agent Loop ===")

    if not SHEET_ID:
        print("❌ TEST_SHEET_ID not set in .env — add it and retry")
        sys.exit(1)

    await test_price_query()
    await test_hours_query()

    print("\n✅✅✅ All Phase 4 checks passed. Agent loop is working.")


if __name__ == "__main__":
    try:
        asyncio.run(run_all())
    except AssertionError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")
        raise
