---
name: tool-design-refactor
description: >
  Refactor WhatsApp AI receptionist agent tools to follow the config-driven
  tool design pattern. Use this skill whenever the task involves: adding a new
  tool, refactoring existing tools, a client needing a different sheet structure
  or calendar config, a duplicate tool being created for a second client, or any
  change to agent/tools.py or ClientConfig that relates to tool behavior.
  This skill is the single source of truth for how tools are structured in this
  codebase — always consult it before writing or modifying any tool function.
---

# Tool Design Pattern — Refactor Skill

## What this skill does

Refactors the agent's tool layer to follow a **config-driven, registry-based**
pattern where:
- One tool function exists per **behavior**, never per client
- Structural variation (sheet columns, tab names, calendar IDs) lives in
  `ClientConfig.tool_config` in Supabase — never in code
- Each client's enabled tools are built dynamically from their config
- The LLM never sees a tool it isn't configured to use

---

## Read first

Before writing any code, read these files in full:

1. `app/agent/tools.py` — current tool implementations
2. `app/clients/models.py` — current ClientConfig shape
3. `app/agent/graph.py` — how tools are currently passed to the LLM
4. `CLAUDE.md` — project conventions (mandatory)

Then identify:
- Which tools already exist and what external service each calls
- Whether any tools are duplicates of each other (same behavior, different client)
- What structural variation exists between clients (column names, sheet IDs, etc.)
- What the current ClientConfig has — and what it's missing

---

## The pattern — three components

### Component 1 — `ClientConfig.tool_config`

`tool_config` is a JSON column in Supabase on the client config table. Its
**presence or absence as a key signals whether a client has that tool enabled**.
Never use a boolean flag — the config IS the flag.

Shape (add to `app/clients/models.py`):

```python
from typing import Any
from pydantic import BaseModel

class StockToolConfig(BaseModel):
    sheet_id: str
    tab: str
    columns: dict[str, str]  # logical_name → actual_sheet_column_header

class AvailabilityToolConfig(BaseModel):
    calendar_id: str
    slot_duration_minutes: int = 30
    timezone: str = "America/Argentina/Cordoba"

class ToolConfig(BaseModel):
    stock: StockToolConfig | None = None
    availability: AvailabilityToolConfig | None = None
    # Add new tool configs here as new tools are introduced

class ClientConfig(BaseModel):
    # ... existing fields ...
    tool_config: ToolConfig = ToolConfig()
```

**Rule:** add a new Pydantic model (e.g. `MenuToolConfig`) for each new tool.
Never use raw `dict[str, Any]` for tool configs — always typed.

---

### Component 2 — Tool functions

Each tool function receives `config: ClientConfig` as its last argument.
It reads structural variation from `config.tool_config.<tool_name>`.

**Canonical shape:**

```python
async def get_stock(product_name: str, config: ClientConfig) -> str:
    """
    Check stock for a product.
    Called when the user asks about availability or quantity of a product.
    """
    cfg = config.tool_config.stock
    if cfg is None:
        return "Este servicio no está disponible."

    try:
        rows = await sheets.get_rows(sheet_id=cfg.sheet_id, tab=cfg.tab)
        col = cfg.columns
        match = next(
            (r for r in rows if r.get(col["product"], "").lower() == product_name.lower()),
            None,
        )
        if not match:
            return f"No encontré información sobre '{product_name}'."
        return f"{match[col['product']]}: {match[col['quantity']]} unidades — ${match[col['price']]}"
    except Exception as e:
        logfire.error("tool.get_stock.error", client_id=config.client_id, error=str(e))
        raise UpstreamError(f"Error consultando stock: {e}") from e
```

**Rules for tool functions:**
- Always guard with `if cfg is None: return "..."` — never assume the config exists
- Never access `config.tool_config.stock` without the None guard
- All user-facing strings in Spanish
- Errors raise `UpstreamError` (from `app/exceptions.py`) — never swallow silently
- Log with `logfire.span` or `logfire.error`, always including `client_id`
- The `config` argument is **never** included in the Anthropic tool schema — it
  is injected by the tool runner, not supplied by the LLM

---

### Component 3 — Tool registry + factory

Add to `app/agent/tools.py`:

```python
from typing import Callable, Any
from app.clients.models import ClientConfig

# --- Registry ---
# Maps tool name → (function, anthropic_schema)
# Add every tool here. graph.py never imports tool functions directly.

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_stock": {
        "fn": get_stock,
        "schema": {
            "name": "get_stock",
            "description": "Consulta el stock disponible de un producto.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Nombre del producto a consultar",
                    }
                },
                "required": ["product_name"],
            },
        },
    },
    "check_availability": {
        "fn": check_availability,
        "schema": {
            "name": "check_availability",
            "description": "Consulta disponibilidad de turnos o citas.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Fecha en formato YYYY-MM-DD",
                    }
                },
                "required": ["date"],
            },
        },
    },
    # Add new tools here
}


def build_tools_for_client(config: ClientConfig) -> list[dict]:
    """
    Return Anthropic tool schemas for tools this client has configured.
    A tool is enabled if its key exists in config.tool_config and is not None.
    """
    enabled = []
    for name, entry in TOOL_REGISTRY.items():
        tool_cfg = getattr(config.tool_config, name, None)
        if tool_cfg is not None:
            enabled.append(entry["schema"])
    return enabled


async def run_tool(
    tool_name: str,
    tool_input: dict,
    config: ClientConfig,
) -> str:
    """
    Dispatch a tool call from the LLM to the correct function.
    Injects config — the LLM never passes it.
    """
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        logfire.warning("tool.unknown", tool_name=tool_name, client_id=config.client_id)
        return f"Herramienta '{tool_name}' no disponible."

    with logfire.span(f"tool.{tool_name}", client_id=config.client_id):
        return await entry["fn"](**tool_input, config=config)
```

---

## How graph.py must use these

In `app/agent/graph.py`, replace any direct tool list construction with:

```python
from app.agent.tools import build_tools_for_client, run_tool

# When building the LLM call:
tools = build_tools_for_client(config)

# When handling a tool_use block from the LLM:
result = await run_tool(
    tool_name=tool_use_block.name,
    tool_input=tool_use_block.input,
    config=config,
)
```

**Never** import individual tool functions in `graph.py`. All routing goes
through `run_tool`.

---

## Refactor procedure — step by step

### Step 1 — Audit existing tools

For each tool in `app/agent/tools.py`, answer:
- Does another tool do the same thing for a different client? → merge them
- Does it hardcode a sheet ID, tab name, or column name? → move to tool_config
- Does it hardcode a calendar ID or timezone? → move to tool_config
- Does it check which client it is with `if client_id == "...":`? → that's a
  structural variation, move it to config

### Step 2 — Update ClientConfig

Add `ToolConfig` and all sub-models to `app/clients/models.py` as shown above.
Add corresponding typed sub-models for each tool found in Step 1.

### Step 3 — Update Supabase seed / migration

Write the `tool_config` JSON for each existing client. Use this as the
canonical shape for a retailer example:

```json
{
  "stock": {
    "sheet_id": "THEIR_SHEET_ID",
    "tab": "Inventario",
    "columns": {
      "product": "Producto",
      "quantity": "Stock",
      "price": "Precio"
    }
  }
}
```

For clients that don't use a tool, omit the key entirely — do not set it to
`null` or `{}`.

Document the per-client JSON in a comment block at the top of
`app/clients/service.py` until there is a proper seed file.

### Step 4 — Rewrite tool functions

For each tool:
1. Remove any client-specific branching (`if client_id == ...`)
2. Replace hardcoded structural values with `config.tool_config.<tool>.columns[...]`
3. Add the None guard at the top
4. Add `logfire.span` wrapping
5. Ensure all user-facing text is in Spanish

### Step 5 — Build the registry

Add all refactored tools to `TOOL_REGISTRY` in the format shown above.
Verify the Anthropic schema for each tool includes only the parameters the
LLM should supply — never `config`, `client_id`, or internal params.

### Step 6 — Update graph.py

Replace tool list construction and dispatch with `build_tools_for_client` and
`run_tool` as shown above.

### Step 7 — Write / update tests

For each tool, add a test in `tests/agent/test_tools.py`:

```python
@pytest.mark.asyncio
async def test_get_stock_found(fake_client_config):
    fake_client_config.tool_config.stock = StockToolConfig(
        sheet_id="x",
        tab="Inventario",
        columns={"product": "Producto", "quantity": "Stock", "price": "Precio"},
    )
    with respx.mock:
        # mock the sheets HTTP call
        ...
        result = await get_stock("Tornillo 5mm", config=fake_client_config)
    assert "Tornillo 5mm" in result

@pytest.mark.asyncio
async def test_get_stock_disabled(fake_client_config):
    # tool_config.stock is None by default in the fixture
    result = await get_stock("anything", config=fake_client_config)
    assert result == "Este servicio no está disponible."

def test_build_tools_for_client_only_enabled(fake_client_config):
    fake_client_config.tool_config.stock = StockToolConfig(...)
    # availability is not set
    tools = build_tools_for_client(fake_client_config)
    names = [t["name"] for t in tools]
    assert "get_stock" in names
    assert "check_availability" not in names
```

---

## Adding a new tool (future use)

When a client needs a new capability:

1. Ask: does any existing tool do the same thing? If yes → extend its config model
2. If genuinely new behavior: add a typed `XxxToolConfig` model to `models.py`
3. Add `xxx: XxxToolConfig | None = None` to `ToolConfig`
4. Write the tool function following the canonical shape above
5. Add it to `TOOL_REGISTRY` with its Anthropic schema
6. Write the Supabase `tool_config` JSON for the client(s) who need it
7. Write the tests (found / not found / disabled)

**Never** create a `get_stock_client_name` or `check_availability_v2` function.
That is always a sign that structural variation should move to config.

---

## Logfire instrumentation rules for tools

```python
# Correct — span per tool call, client_id as attribute
with logfire.span("tool.get_stock", client_id=config.client_id):
    ...

# Correct — error with context, not in message string
logfire.error("tool.get_stock.error", client_id=config.client_id, error=str(e))

# Wrong — client_id in the message string
logfire.info(f"get_stock called for {config.client_id}")

# Wrong — no span, no client_id
logfire.info("tool called")
```

Span name format: `tool.{tool_name}` — consistent with the rest of the
project's `{domain}.{action}` convention.

---

## Invariants — never violate these

- One tool function per behavior. Never per client.
- `config` is always the last argument of a tool function and always has a
  type annotation.
- `config` is never in the Anthropic tool schema.
- A tool function never imports from `graph.py`.
- `graph.py` never imports individual tool functions — only `run_tool` and
  `build_tools_for_client`.
- A tool with no `tool_config` entry for a client returns a Spanish string —
  it never raises, never 500s.
- All new tools are registered in `TOOL_REGISTRY` before being wired anywhere.