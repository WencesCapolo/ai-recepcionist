# Tool Design — Config-Driven Pattern

## The Problem This Solves

Every client has different tools, sheet structures, calendar IDs, and payment credentials. Without a pattern, each new client means new code: new tool functions, new branches, duplicated logic. This doc describes the pattern that prevents that.

**Rule:** one tool function per behavior, never per client. All structural variation lives in config.

---

## How the Three Pieces Fit Together

```
Supabase clients table
  └─ tools_enabled: ["get_price", "get_stock", "get_hours"]
  └─ tool_config: {retail: {sheet_id: "...", tab: "productos"}, payment: {...}}
        │
        ▼
  _build_config() in service.py
  Maps flat columns → ClientConfig.tool_config
        │
        ▼
  build_tools_for_client(config, ...)
  For each enabled tool whose tool_config entry is present:
    → build handler closure (captures sheet_id, column names, etc.)
    → return [{definition, handler}]
        │
        ▼
  graph.py → LLM sees only the schemas
           → run_tool() dispatches calls to the right handler
```

---

## `ClientConfig.tool_config` Shape

`ToolConfig` in `app/clients/models.py`:

```python
class ToolConfig(BaseModel):
    retail:        RetailToolConfig | None = None
    calendar:      CalendarToolConfig | None = None
    dentist_sheets: DentistSheetsConfig | None = None
    padel:         PadelConfig | None = None
    payment:       PaymentConfig | None = None
```

**Presence of a sub-model is the gate.** No boolean flags. If `tool_config.retail` is `None`, no retail tools are built — no guard needed in `build_tools_for_client`.

### Sub-model reference

| Model | Gates | Key fields |
|---|---|---|
| `RetailToolConfig` | `get_price`, `get_stock`, `get_all_products`, `get_products_by_category`, `generate_payment_link` | `sheet_id`, `tab`, `columns` |
| `CalendarToolConfig` | `check_availability`, `book_appointment`, `get_appointment`, `cancel_appointment`, `reschedule_appointment`, `get_treatment_info` | `calendar_id` (None → mock), `slot_minutes`, `work_start/end`, `work_days` |
| `DentistSheetsConfig` | `get_treatment_info`, `get_prices`, `get_insurances` | `sheet_id`, `tab_treatments`, `tab_insurances` |
| `PadelConfig` | `get_availability`, `create_booking`, `cancel_booking`, `generate_padel_payment_link` | `calendar_id`, `slot_minutes` |
| `PaymentConfig` | `generate_payment_link`, `generate_padel_payment_link` | `access_token`, `sandbox` |
| `ResellerConfig` | `get_reseller` | `sheet_id`, `tab`, `columns` |

`get_hours` and `get_current_date_hour` have **no gate** — they are built unconditionally from the system prompt / current time.

---

## Tool Functions — Canonical Shape

```python
# app/agent/verticals/retail.py

def _make_get_price(cfg: RetailToolConfig, sheets: SheetsClient, config: ClientConfig) -> dict:
    async def handler(product: str) -> str:
        with logfire.span("tool.get_price", client_id=str(config.id)):
            col = cfg.columns
            rows = sheets.find_products(cfg.sheet_id, product, worksheet=cfg.tab)
            if not rows:
                return f"No encontré '{product}' en el catálogo."
            ...
    return {"definition": {...}, "handler": handler}
```

**Rules:**
- `cfg` (the tool's sub-config) is passed into the factory, not read from `config` inside the handler — the handler closes over typed values, not the full config.
- All user-facing strings in Spanish.
- Every handler has exactly one `logfire.span("tool.<name>", client_id=...)` wrapping its work.
- Errors log with `logfire.error("tool.<name>.error", client_id=..., error=str(e))` and return a Spanish string — they never raise.

---

## `build_tools_for_client` — the Assembly Point

`app/agent/tools.py`:

```python
def build_tools_for_client(config, sheets, redis=None, user_phone="", client_id="") -> list[dict]:
    all_tools: dict[str, dict] = {}

    # Shared tools — always available
    all_tools["get_current_date_hour"] = make_get_current_date_hour()
    all_tools["get_hours"] = make_get_hours(config)

    # Vertical tools — each builder checks its own tool_config gate
    for tool in build_retail_tools(config, sheets=sheets, redis=redis, user_phone=user_phone):
        all_tools[tool["definition"]["name"]] = tool
    ...

    return [all_tools[name] for name in config.tools_enabled if name in all_tools]
```

The output list is ordered by `tools_enabled`. The LLM sees only the tools in that list, in that order.

---

## `run_tool` — the Dispatcher

`app/agent/tools.py`:

```python
async def run_tool(tool_name: str, tool_input: dict, handler_map: dict) -> str:
    handler = handler_map.get(tool_name)
    if handler is None:
        return f"Herramienta '{tool_name}' no disponible."
    return await handler(**tool_input)
```

`graph.py` builds `handler_map` from the output of `build_tools_for_client` and passes it through `_AgentState`. The `tools_node` in the graph calls `run_tool` — it never calls handlers directly.

---

## Migration Adapter — Supabase Flat Columns

Until the `clients` table has a `tool_config` JSONB column, `_build_config()` in `app/clients/service.py` assembles `tool_config` from legacy flat columns:

| Legacy flat column | Maps to |
|---|---|
| `sheet_id` | `tool_config.retail.sheet_id` |
| `sheet_tab_products` | `tool_config.retail.tab` |
| `mp_access_token` | `tool_config.payment.access_token` |
| `mp_sandbox` | `tool_config.payment.sandbox` |
| `calendar_id` (when `check_availability` in tools) | `tool_config.calendar.calendar_id` |
| `calendar_id` (when `get_availability` in tools) | `tool_config.padel.calendar_id` |
| `slot_minutes`, `work_start_hour`, `work_end_hour`, `work_days` | `tool_config.calendar.*` |
| `prices_sheet_id` | `tool_config.dentist_sheets.sheet_id` |
| `sheet_tab_treatments`, `sheet_tab_insurances` | `tool_config.dentist_sheets.*` |

When the Supabase `tool_config` JSONB column is populated for a client, `_build_config` uses it directly and the flat columns are ignored.

---

## Current Clients — `tool_config` Reference

### Ferretería Stainless

```json
{
  "retail": {
    "sheet_id": "1LPPx8pe250W4qVWxR_ROGBHapAy-PS0BMr8iQ-M43oo",
    "tab": "productos"
  },
  "payment": { "access_token": "APP_USR-...", "sandbox": true }
}
```

`tools_enabled`: `["get_price", "get_stock", "get_all_products", "get_hours", "get_products_by_category", "generate_payment_link"]`

---

### Consultorio Odontológico Martinez

```json
{
  "calendar": {
    "calendar_id": "martinez@gmail.com",
    "slot_minutes": 30,
    "work_start": 9,
    "work_end": 18
  },
  "dentist_sheets": {
    "sheet_id": "...",
    "tab_treatments": "Tratamientos",
    "tab_insurances": "Obras Sociales"
  },
  "payment": { "access_token": "APP_USR-...", "sandbox": true }
}
```

`tools_enabled`: `["get_current_date_hour", "get_treatment_info", "check_availability", "book_appointment", "get_appointment", "cancel_appointment", "reschedule_appointment", "get_hours", "get_prices", "get_insurances", "generate_payment_link"]`

---

### Tuti Bakery

```json
{
  "retail": {
    "sheet_id": "...",
    "tab": "productos"
  },
  "payment": { "access_token": "APP_USR-...", "sandbox": true }
}
```

`tools_enabled`: `["get_price", "get_stock", "get_all_products", "get_hours"]`

---

### Todo Padel

```json
{
  "padel": { "calendar_id": "...", "slot_minutes": 60 },
  "payment": { "access_token": "APP_USR-...", "sandbox": true }
}
```

`tools_enabled`: `["get_current_date_hour", "get_availability", "create_booking", "cancel_booking", "get_price", "get_hours", "generate_padel_payment_link"]`

---

### Quimexur Pinturas

```json
{
  "retail": {
    "sheet_id": "<productos_sheet_id>",
    "tab": "productos"
  },
  "reseller": {
    "sheet_id": "<sheet_id>",
    "tab": "revendedores"
  },
  "payment": { "access_token": "APP_USR-...", "sandbox": true }
}
```

`tools_enabled`: `["get_price","get_stock","get_all_products","get_hours","get_products_by_category","generate_payment_link","get_reseller"]`

---

## Adding a New Client

1. Insert a row into `clients` with their `system_prompt`, `tools_enabled`, and the legacy flat columns (or a `tool_config` JSONB if the column exists).
2. Done — no code changes needed if they only use existing tool behaviors.

If they need a behavior that doesn't exist yet, see the next section.

---

## Adding a New Tool

1. **Check first:** does any existing tool do the same thing? If yes, extend its config model with the new structural variation — don't create a new function.

2. **If genuinely new behavior:**

   a. Add a typed `XxxToolConfig` model to `app/clients/models.py`:
   ```python
   class XxxToolConfig(BaseModel):
       some_field: str
       another_field: int = 10

   class ToolConfig(BaseModel):
       ...
       xxx: XxxToolConfig | None = None
   ```

   b. Add the tool name to `KNOWN_TOOL_NAMES` in `app/agent/registry.py`.

   c. Write the tool factory in the appropriate vertical (or a new one in `app/agent/verticals/`):
   ```python
   def _make_xxx_tool(cfg: XxxToolConfig, config: ClientConfig) -> dict:
       async def handler(param: str) -> str:
           with logfire.span("tool.xxx", client_id=str(config.id)):
               if cfg is None:
                   return "Este servicio no está disponible."
               ...
       return {"definition": {"name": "xxx", ...}, "handler": handler}
   ```

   d. Register the vertical builder in `build_tools_for_client` in `app/agent/tools.py`.

   e. Update `_build_config` in `app/clients/service.py` to map any new flat columns.

   f. Add the client's JSON to the reference section above.

3. **Write tests** (found / not found / disabled):
   ```python
   @pytest.mark.asyncio
   async def test_xxx_disabled(fake_config):
       # tool_config.xxx is None → should return Spanish unavailable message
       fake_config.tool_config.xxx = None
       result = await handler(param="anything")
       assert result == "Este servicio no está disponible."
   ```

---

## File Map

| File | Role |
|---|---|
| `app/clients/models.py` | `ToolConfig` hierarchy — the single source of structural config |
| `app/clients/service.py` | `_build_config()` — maps Supabase rows to `ClientConfig` |
| `app/agent/registry.py` | `KNOWN_TOOL_NAMES` — used to validate `tools_enabled` |
| `app/agent/shared_tools.py` | `get_current_date_hour`, `get_hours` — no config gate |
| `app/agent/verticals/retail.py` | Retail/bakery tool factories |
| `app/agent/verticals/calendar_booking.py` | Calendar vertical — instantiates calendar client |
| `app/agent/calendar_tools.py` | Calendar tool factories (check_availability, book_appointment, …) |
| `app/agent/verticals/dentist.py` | Dentist info tool factories |
| `app/agent/verticals/padel.py` | Padel vertical — instantiates padel calendar client |
| `app/agent/padel_tools.py` | Padel tool factories (get_availability, create_booking, …) |
| `app/agent/tools.py` | `build_tools_for_client`, `run_tool` — the public API |
| `app/agent/graph.py` | Only caller of `build_tools_for_client` and `run_tool` |
