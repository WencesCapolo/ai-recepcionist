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