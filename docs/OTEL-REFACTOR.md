# OpenTelemetry Observability Refactor

## What was added

Full request traceability and wide-event logging per WhatsApp message. Every inbound message now produces:

1. **One OTel trace** — a `handle_message` root span with all pipeline steps as children.
2. **One wide event** — a single structured JSON log line with full business context.

---

## Deployment

### New environment variables

All new variables are optional with safe defaults. Existing deployments continue to work without them.

```
SERVICE_VERSION=1.0.0        # shown on every span and log line (default: "unknown")
GIT_COMMIT=                  # set in Railway/CI via: git rev-parse --short HEAD
ENVIRONMENT=production       # production | staging | development (default: "development")
RAILWAY_REGION=us-west1      # or set REGION manually (default: "unknown")
```

**Railway setup (Build Command or nixpacks.toml):**
```bash
export GIT_COMMIT=$(git rev-parse --short HEAD)
```

No new required variables. No migrations. Deploy is safe.

---

## Span tree

Every message produces this trace in Logfire / Jaeger / Grafana Tempo:

```
handle_message  [root, ~500ms–5s]
│  message_id=wamid.xxx  client_id=uuid  outcome=success
│
├── dedup_check  [~1ms]
├── client_lookup  [1ms cache / 60ms Supabase]
│     client_id=uuid  client_name="Padel Club"
├── audio_transcription  [~800ms, only for voice messages]
├── redis.history_load  [~5ms]
│     cache_hit=true  message_count=4
├── redis.lock_acquire  [~1ms]
│     wait_attempts=1  acquired=true
├── agent_run  [~1s]
│   ├── agent_iteration[0]  [LLM call]
│   │     finish_reason=tool_calls  tool_count=1
│   │     └── POST api.openai.com  [auto-traced]
│   ├── tool_call[get_products]  [~120ms]
│   │     success=true  result_length=842
│   │     └── GET sheets.googleapis.com  [auto-traced]
│   └── agent_iteration[1]  [final reply]
│         finish_reason=stop
│         └── POST api.openai.com  [auto-traced]
├── redis.history_save  [~5ms]
│     message_count=6
└── whatsapp_send  [~200ms]
    │   reply_length=142
    └── POST graph.facebook.com  [auto-traced]

[background] db.persist_conversation
    messages_logged=2
```

---

## Wide event (one per message)

Emitted at end of every request (including errors and early exits) via the `ai-recepcionist` logger:

```json
{
  "timestamp": "2026-04-06T15:32:11Z",
  "service": "ai-recepcionist",
  "version": "1.0.0",
  "commit_hash": "452eeaa",
  "environment": "production",
  "region": "us-west1",
  "message_id": "wamid.xxx",
  "client_id": "uuid-abc",
  "client_name": "Padel Club Córdoba",
  "inbound_number": "+54911...",
  "user_phone_hash": "7f3a9c",
  "outcome": "success",
  "tools_used": ["get_products"],
  "iterations": 2,
  "reply_length": 142,
  "latency_ms": 1843,
  "latency_breakdown": {
    "agent_run_ms": 1490,
    "whatsapp_send_ms": 210
  },
  "error": null
}
```

### Outcome values

| outcome | meaning |
|---------|---------|
| `success` | full pipeline completed |
| `agent_error` | agent raised but fallback was sent |
| `skipped_duplicate` | same `message_id` seen within 60s |
| `skipped_no_client` | inbound number has no active client |
| `skipped_no_text` | audio transcription failed |
| `skipped_buffer_drained` | another handler won the lock race |
| `error_lock_timeout` | lock not acquired after 12s |
| `error` | unhandled exception |

### Privacy

`user_phone_hash` = first 8 chars of `sha256(user_phone)`. Enough to correlate a session, not enough to identify.

---

## Useful queries (Logfire / any OTel backend)

```sql
-- Slow requests
WHERE service = 'ai-recepcionist' AND latency_ms > 5000

-- Tool failure rate by client
WHERE tools_used contains 'book_appointment' AND outcome = 'agent_error'
GROUP BY client_name

-- Daily message volume per client
GROUP BY client_name, date(timestamp)
COUNT(*)

-- P99 latency per client (SLA monitoring)
PERCENTILE(latency_ms, 99) BY client_name

-- Error audit
WHERE outcome = 'error' OR outcome = 'agent_error'
ORDER BY timestamp DESC
```

---

## Files changed

| File | Change |
|------|--------|
| `app/observability.py` | **NEW** — `WideEvent` builder, `ENV_CONTEXT`, single logger |
| `app/main.py` | `logfire.configure()` now sets `service_name` + `service_version` |
| `app/webhook/handler.py` | Root span wraps full pipeline; `WideEvent` threaded through; `TurnResult` extended with `tools_used`, `iterations`, `agent_ms`, `send_ms` |
| `app/agent/graph.py` | `run_agent` returns `(reply, list[str], int)`; spans enriched with `client_id`, `finish_reason`, `tool_count`, `result_length`, `success` |
| `app/context/redis.py` | `redis.history_load`, `redis.history_save`, `redis.lock_acquire` spans with cache/timing attributes |
| `app/webhook/handler.py` | `db.persist_conversation` span with `messages_logged` |
| `app/config.py` | New optional settings: `service_version`, `git_commit`, `railway_region` |
| `.env.example` | Documents new optional vars |
| `tests/test_observability.py` | **NEW** — 22 unit tests for `WideEvent` and `ENV_CONTEXT` |
| `tests/test_handler_observability.py` | **NEW** — 9 integration tests for handler wiring |
| `tests/smoke_phase5.py` | Fixed pre-existing bugs: wrong method name (`get_by_phone` → `get_client_by_phone`), `SecretStr` patching, `run_agent` mock return type |

---

## What is NOT traced (by design)

- `app/integrations/whatsapp.py`, `sheets.py`, `calendar.py`, `mercadopago.py` — their outbound HTTP calls are **already traced** by `logfire.instrument_httpx()`. Adding manual spans would create duplicate noise.
