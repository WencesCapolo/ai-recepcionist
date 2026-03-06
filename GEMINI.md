# AI Receptionist — Agent Context

## What this is
WhatsApp AI receptionist for small businesses in Córdoba, Argentina.
Multi-tenant: one deployment, N clients, config-driven via Supabase.

## Stack
- FastAPI (async webhooks)
- LangGraph (agent loop only — NOT LangChain)
- Anthropic Claude Haiku (primary LLM)
- Upstash Redis (conversation memory, locking, dedup)
- Supabase Postgres (client configs, conversation logs)
- Google Sheets via gspread (per-client data source)
- Railway (deployment)

## Critical conventions
- NEVER use LangChain. LangGraph only for the agent loop.
- All state is owned manually in Redis/Supabase — not by LangGraph.
- Tools use native Anthropic function calling (not LangChain tool wrappers).
- Redis key format: `history:{client_id}:{user_phone}`, `lock:{...}`, `dedup:{...}`
- All Supabase writes are async background tasks (never block the response).
- All user-facing error messages must be in Spanish.

## File responsibilities
- main.py — FastAPI app and webhook handler only
- agent.py — LangGraph graph definition
- tools.py — all tool functions (Sheets, config reads)
- context.py — Redis: load/save history, lock, TTL
- clients.py — Supabase: load client config + Redis cache
- whatsapp.py — Meta API: send_message only
- dedup.py — Redis SET NX deduplication

## Current phase
MVP. See /docs/context.md for full architecture spec.

## Project Structure

Feature/domain folders. Each domain is self-contained.

app/
  /webhook
    router.py       # FastAPI route definitions
    handler.py      # orchestrates full request lifecycle
  /agent
    graph.py        # LangGraph graph definition
    tools.py        # tool functions
    prompts.py      # system prompt builders
  /clients
    service.py      # business logic + Supabase queries (split when file earns it)
    models.py       # Pydantic models
  /conversations
    service.py      # upsert conversation, append messages + Supabase queries
    models.py       # Pydantic models
  /context
    redis.py        # history load/save, locking, dedup
  /integrations
    sheets.py       # Google Sheets client
    whatsapp.py     # Meta API client
  config.py         # Pydantic settings model
  main.py           # app factory, register routers

## Patterns

**Repository pattern:** Supabase calls as methods on a class, never inline scattered functions.

**Settings via Pydantic BaseSettings:** all env vars in config.py, validated on startup. App must crash immediately if a required env var is missing.

**Dependency injection:** instantiate clients (Supabase, Redis) as FastAPI Depends, never inside functions.

**One Pydantic model per entity** in its own models.py. Validate at the boundary.

## Rules

- Split service.py into service.py + repository.py only when the file grows past ~100 lines and naturally wants to split. Not before.
- No abstract base classes until multiple implementations exist.
- No SQLAlchemy or ORM. Use supabase-py for all data access.
- No global mutable state. Everything scoped to the request.
- All Redis keys follow the pattern: {purpose}:{client_id}:{user_phone}