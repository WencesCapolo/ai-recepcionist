# tests/smoke_phase3.py
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from app.integrations.sheets import SheetsClient
from app.agent.tools import build_tools_for_client
from app.clients.models import ClientConfig, ToolConfig, RetailToolConfig
import uuid

# Replace with your real sheet ID
SHEET_ID = os.getenv("TEST_SHEET_ID", "")
CLIENT_ID = os.getenv("TEST_CLIENT_ID", str(uuid.uuid4()))

def make_test_config() -> ClientConfig:
    return ClientConfig(
        id=uuid.UUID(CLIENT_ID),
        name="Ferretería Stainless",
        whatsapp_number="+5493511234567",
        system_prompt="""Sos el asistente de Ferretería Stainless.
Horario: lunes a viernes 8:00–18:00, sábados 8:00–13:00, domingos cerrado.""",
        tools_enabled=["get_price", "get_stock", "get_all_products", "get_hours"],
        prompt_version=1,
        active=True,
        tool_config=ToolConfig(
            retail=RetailToolConfig(sheet_id=SHEET_ID),
        ),
    )


def test_sheets():
    print("\n--- sheets.py ---")
    client = SheetsClient()

    rows = client.get_all_rows(SHEET_ID)
    assert len(rows) > 0, "❌ No rows returned — check sheet_id and service account permissions"
    print(f"✅ Loaded {len(rows)} products from sheet")

    row = client.find_product(SHEET_ID, "tornillo")
    assert row is not None, "❌ Could not find 'tornillo' — check sheet data"
    print(f"✅ Found product: {row['producto']} @ ${row['precio']}")

    missing = client.find_product(SHEET_ID, "producto que no existe xyz")
    assert missing is None
    print("✅ Missing product correctly returns None")


def test_tools():
    print("\n--- tools.py ---")
    config = make_test_config()
    sheets = SheetsClient()
    tools = build_tools_for_client(config, sheets)

    assert len(tools) == 4, f"❌ Expected 4 tools, got {len(tools)}"
    tool_names = [t["definition"]["name"] for t in tools]
    print(f"✅ Built {len(tools)} tools: {tool_names}")

    # Map name → handler for easy testing
    handlers = {t["definition"]["name"]: t["handler"] for t in tools}

    # get_price
    price_result = handlers["get_price"]("tornillo 6x50")
    assert "$" in price_result, f"❌ Price result missing $: {price_result}"
    print(f"✅ get_price: {price_result}")

    # get_price — missing product
    missing_result = handlers["get_price"]("producto inexistente xyz")
    assert "No encontré" in missing_result
    print(f"✅ get_price (missing): {missing_result}")

    # get_stock
    stock_result = handlers["get_stock"]("tornillo 6x50")
    assert "stock" in stock_result.lower() or "unidad" in stock_result.lower()
    print(f"✅ get_stock: {stock_result}")

    # get_all_products
    catalog = handlers["get_all_products"]()
    assert "Tornillería" in catalog or "tornillo" in catalog.lower()
    print(f"✅ get_all_products: returned {len(catalog)} chars")

    # get_hours
    hours = handlers["get_hours"]()
    assert "lunes" in hours.lower() or "horario" in hours.lower()
    print(f"✅ get_hours: {hours}")


if __name__ == "__main__":
    print("=== Phase 3 Smoke Test ===")
    try:
        if not SHEET_ID:
            print("❌ TEST_SHEET_ID not set in .env — add it and retry")
            sys.exit(1)
        test_sheets()
        test_tools()
        print("\n✅✅✅ All Phase 3 checks passed. Ready for Phase 4.")
    except AssertionError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 {e}")
        raise
