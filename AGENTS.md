# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

WhatsApp AI receptionist for small businesses in Córdoba, Argentina. Multi-tenant: one deployment, N clients, config-driven via Supabase.

## Commands

```bash
# Run the app locally
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Run all tests
pytest tests/

# Run a single test file
pytest tests/smoke_phase5.py -v

# Run with asyncio mode (already configured in pyproject.toml)
pytest tests/ -v

# Install dependencies
pip install -e ".[dev]"
```

## Architecture

**Request lifecycle (webhook POST):**
1. Meta sends webhook → FastAPI returns 200 immediately
2. All processing runs in `BackgroundTasks` (never block the response)
3. Dedup check (Redis SET NX, 60s TTL)
4. Debounce buffer (Redis, 3s) — batches rapid-fire messages
5. Acquire lock (`lock:{client_id}:{user_phone}`, 10s TTL, SET NX)
6. Load client config from Supabase (cached in Redis 5 min)
7. Load conversation history from Redis (`history:{client_id}:{user_phone}`, 24h TTL)
8. Run LangGraph agent loop (GPT-4o-mini, max 5 iterations)
9. Send reply via Meta Cloud API
10. Save history to Redis + background-persist to Supabase

**Multi-tenancy:** Client identified by inbound WhatsApp business number. Per-client config in Supabase `clients` table includes `system_prompt`, `tools_enabled`, and a `tool_config` JSONB column (or legacy flat columns mapped by `_build_config` in `clients/service.py`).

**Agent tools** are built dynamically per-client from `tools_enabled` + `tool_config`. One function per behavior — never per client. Structural variation (sheet IDs, column names, calendar IDs) lives in `ClientConfig.tool_config` sub-models, not in code. See `docs/tool-design.md` for the full pattern.

**Payment flow:** `generate_payment_link` tool creates checkout URL → user pays → Mercado Pago POSTs to `/webhook/mp` → handler confirms payment, sends notification to user.

## Stack

- **FastAPI** — async webhooks (`app/webhook/`)
- **LangGraph** — agent loop only, NOT LangChain (`app/agent/graph.py`)
- **OpenAI GPT-4o-mini** — primary LLM
- **Upstash Redis** — conversation history, locking, dedup, debounce
- **Supabase Postgres** — client configs, conversation logs
- **Google Sheets** via gspread — per-client product/service catalog
- **Logfire** — observability (wide events, per-tool spans)
- **Railway** — deployment

## Critical conventions

- **NEVER use LangChain.** LangGraph only for the agent loop.
- All state is owned manually in Redis/Supabase — not by LangGraph.
- Tools use native OpenAI function calling (not LangChain tool wrappers).
- All Supabase writes are async background tasks (never block the response).
- All user-facing error messages must be **in Spanish**.
- No global mutable state. Everything scoped to the request.
- No SQLAlchemy or ORM — use supabase-py for all data access.
- No abstract base classes until multiple implementations exist.
- Split `service.py` into `service.py` + `repository.py` only when the file grows past ~100 lines.

## Tool layer conventions

- **One tool function per behavior, never per client.** Structural variation lives in `ClientConfig.tool_config`, not in code.
- **`ClientConfig.tool_config`** is a `ToolConfig` Pydantic model. Its sub-model presence gates the tool: `tool_config.retail` enables retail tools, `tool_config.calendar` enables calendar tools, etc.
- **`build_tools_for_client(config, sheets, redis, user_phone)`** in `app/agent/tools.py` is the only place that assembles the tool list. `graph.py` calls this — nothing else does.
- **`run_tool(tool_name, tool_input, handler_map)`** in `app/agent/tools.py` is the only dispatcher. `graph.py` calls this — nothing else does.
- **`graph.py` never imports individual tool functions.** Only `build_tools_for_client` and `run_tool`.
- **Shared tools** (`get_current_date_hour`, `get_hours`) live in `app/agent/shared_tools.py`. They have no `tool_config` gate.
- Every tool handler wraps its work in `logfire.span("tool.<name>", client_id=...)`.
- A tool with no config entry returns a Spanish string — it never raises.
- See `docs/tool-design.md` for the full pattern, config shapes, and the "adding a new tool" procedure.

## Redis key patterns

```
history:{client_id}:{user_phone}   # Conversation history (24h TTL)
lock:{client_id}:{user_phone}      # Processing lock (10s TTL)
dedup:{message_id}                 # Deduplication (60s TTL)
buffer:{client_id}:{user_phone}    # Debounce message buffer (3s TTL)
debounce:{client_id}:{user_phone}  # Last buffer activity tracker
```

## Patterns to follow

- **Repository pattern:** Supabase calls as methods on a service class, never inline.
- **Pydantic BaseSettings:** all env vars in `app/config.py`, app must crash on startup if a required var is missing.
- **Dependency injection:** instantiate Supabase/Redis clients as FastAPI `Depends`, never inside functions. See `app/dependencies.py`.
- **One Pydantic model per entity** in its domain's `models.py`. Validate at the boundary.

## Environment variables

Copy `.env.example` to `.env`. Key vars: `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (base64-encoded).
