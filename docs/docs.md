# AI Receptionist — System Flows

## Table of Contents
1. [Webhook Request Lifecycle](#1-webhook-request-lifecycle)
2. [Multi-Tenancy & Client Identification](#2-multi-tenancy--client-identification)
3. [Deduplication & Locking](#3-deduplication--locking)
4. [Conversation Memory](#4-conversation-memory)
5. [Agent Loop](#5-agent-loop)
6. [Background Persistence](#6-background-persistence)
7. [Full System Map](#7-full-system-map)

---

## 1. Webhook Request Lifecycle

Every inbound WhatsApp message follows this exact sequence. Steps 1–8 are on the critical path (user is waiting). Step 9 runs in the background after the reply is sent.

```mermaid
sequenceDiagram
    actor User as 👤 User (WhatsApp)
    participant Meta as Meta API
    participant Railway as Railway (FastAPI)
    participant Redis as Upstash Redis
    participant Supabase as Supabase
    participant LLM as Claude Haiku
    participant Sheets as Google Sheets

    User->>Meta: Sends WhatsApp message
    Meta->>Railway: POST /webhook (JSON payload)
    Railway-->>Meta: HTTP 200 (immediate — Meta requires this)

    Railway->>Railway: Extract message_id, user_phone, text, inbound_number

    Railway->>Redis: SET NX dedup:{message_id} (TTL 60s)
    alt Already processed
        Redis-->>Railway: key existed → duplicate
        Railway->>Railway: return silently
    end

    Railway->>Redis: GET config:{inbound_number}
    alt Cache miss
        Railway->>Supabase: SELECT * FROM clients WHERE whatsapp_number = ?
        Supabase-->>Railway: ClientConfig row
        Railway->>Redis: SET config:{inbound_number} (TTL 5min)
    end

    alt No active client found
        Railway->>Railway: return silently
    end

    Railway->>Redis: SET NX lock:{client_id}:{user_phone} (TTL 10s)
    alt Lock already held
        Redis-->>Railway: concurrent message in flight
        Railway->>Railway: return silently
    end

    Railway->>Redis: GET history:{client_id}:{user_phone}
    Redis-->>Railway: ConversationHistory (or empty on miss)

    Railway->>LLM: messages + tools + system prompt
    loop Tool calls (max 5 iterations)
        LLM-->>Railway: tool_use block
        Railway->>Sheets: query product data
        Sheets-->>Railway: result
        Railway->>LLM: tool_result
    end
    LLM-->>Railway: final text reply

    Railway->>Meta: POST /messages (send reply to user)
    Meta->>User: WhatsApp message delivered

    Railway->>Redis: SET history:{client_id}:{user_phone} (TTL 24h reset)
    Railway->>Redis: DEL lock:{client_id}:{user_phone}

    Note over Railway,Supabase: BackgroundTask (non-blocking)
    Railway-)Supabase: UPSERT conversation + INSERT messages
```

---

## 2. Multi-Tenancy & Client Identification

One deployed codebase serves N clients. The client is identified at request time from the inbound phone number — no routing config, no code changes needed to add a client.

```mermaid
flowchart TD
    A[Webhook arrives] --> B{Extract inbound_number\nfrom payload metadata}

    B --> C[Check Redis\nconfig:inbound_number]

    C --> D{Cache hit?}

    D -- Yes --> G[ClientConfig loaded\nfrom Redis]
    D -- No --> E[Query Supabase\nclients table]

    E --> F{Active client\nfound?}

    F -- No --> X[Drop silently\nreturn]
    F -- Yes --> G2[Write to Redis\nTTL 5min]
    G2 --> G

    G --> H{Which tools\nare enabled?}

    H --> I[Build tool list\nfrom tools_enabled array]
    H --> J[Load system_prompt\nfor this client]

    I --> K[Run agent with\nclient-specific config]
    J --> K

    subgraph Supabase clients table
        R1[Ferretería Stainless\n+5493511234567\ntools: price, stock, hours]
        R2[Peluquería Sol\n+5493519876543\ntools: price, hours]
        R3[Veterinaria XYZ\n+5493518765432\ntools: price, hours, calendar]
    end
```

**Key rule:** Adding a new client = one `INSERT` into `clients`. Zero code changes. Zero redeploys.

---

## 3. Deduplication & Locking

Two separate Redis mechanisms protect against two different problems.

```mermaid
flowchart LR
    subgraph Problem 1 — Meta retries the same message
        A1[Message arrives\nmessage_id = wamid.abc] --> B1[SET NX dedup:wamid.abc\nTTL 60s]
        B1 --> C1{Key existed?}
        C1 -- No, inserted → first time --> D1[Process message ✅]
        C1 -- Yes, existed → retry --> E1[Drop silently ✅]
    end

    subgraph Problem 2 — User sends two messages in 200ms
        A2[Message 1 arrives] --> B2[SET NX lock:client:phone\nTTL 10s]
        B2 --> C2{Lock acquired?}
        C2 -- Yes --> D2[Process message 1\nhold lock]
        D2 --> E2[Release lock\nDEL lock:client:phone]

        A3[Message 2 arrives\n200ms later] --> B3[SET NX lock:client:phone]
        B3 --> C3{Lock acquired?}
        C3 -- No, locked --> F3[Drop silently ✅]
    end
```

**Why two mechanisms?**

| | Dedup | Lock |
|---|---|---|
| Protects against | Meta webhook retries | Concurrent msgs from same user |
| Key | `dedup:{message_id}` | `lock:{client_id}:{user_phone}` |
| TTL | 60s | 10s |
| On conflict | Drop | Drop |
| Fail behavior | Fail open (process anyway) | Fail closed (drop) |

---

## 4. Conversation Memory

Redis is the working memory. Supabase is the permanent log. They serve different purposes and are never used interchangeably.

```mermaid
stateDiagram-v2
    [*] --> FirstMessage: User sends first message

    FirstMessage --> RedisLoad: Load history:{client_id}:{user_phone}
    RedisLoad --> EmptyHistory: Redis miss (new conversation\nor 24h TTL expired)
    RedisLoad --> ExistingHistory: Redis hit (active conversation)

    EmptyHistory --> AgentRun: Start fresh context
    ExistingHistory --> AgentRun: Resume with full context

    AgentRun --> AgentReply: LLM produces reply

    AgentReply --> AppendMessages: Append user + assistant\nmessages to history

    AppendMessages --> RedisSave: SET history:{client_id}:{user_phone}\nTTL reset to 24h

    RedisSave --> [*]: Next message resumes here

    RedisSave --> SupabaseLog: BackgroundTask\n(non-blocking)
    SupabaseLog --> [*]

    note right of EmptyHistory
        Option A (MVP):
        No reload from Supabase.
        Fresh start after 24h inactivity.
        Option B (Week 3-4):
        On Redis miss, reload last N
        messages from Supabase.
    end note
```

**Redis key TTL behavior:**

```
User sends msg at 10:00 → history TTL set to 24h → expires 10:00 next day
User sends msg at 14:00 → history TTL reset to 24h → expires 14:00 next day
User sends msg at 23:50 → history TTL reset to 24h → expires 23:50 next day
User goes silent for 25h → key expires → next message starts fresh
```

---

## 5. Agent Loop

The LangGraph graph is a simple loop with a hard iteration cap. No LangGraph state persistence — all state enters and exits as plain Python objects.

```mermaid
flowchart TD
    IN[Input:\nClientConfig\nConversationHistory\nuser_message\nSheetsClient] --> PROMPT[Build system prompt\nfrom config.system_prompt\n+ behavior rules block]

    PROMPT --> MSGLIST[Convert history\nto Anthropic messages list]

    MSGLIST --> TOOLS[Build tool list\nfrom config.tools_enabled]

    TOOLS --> LLM[Call Claude Haiku\nwith messages + tools]

    LLM --> RESP{Response type?}

    RESP -- text, no tool_use --> OUT[Return reply string ✅]

    RESP -- tool_use block --> FIND[Find handler\nby tool name]

    FIND --> CALL{Which tool?}

    CALL -- get_price --> SHEETS1[sheets.find_product\nreturn price string]
    CALL -- get_stock --> SHEETS2[sheets.find_product\nreturn stock string]
    CALL -- get_all_products --> SHEETS3[sheets.get_all_rows\nreturn catalog string]
    CALL -- get_hours --> CONFIG1[Read from\nsystem_prompt line]

    SHEETS1 --> TOOLRESULT[Append tool_result\nmessage to context]
    SHEETS2 --> TOOLRESULT
    SHEETS3 --> TOOLRESULT
    CONFIG1 --> TOOLRESULT

    TOOLRESULT --> ITER{Iterations < 5?}
    ITER -- Yes --> LLM
    ITER -- No → safety limit --> FALLBACK[Return fallback\nmessage in Spanish]
```

**Example multi-step tool call:**

```
User:  "cuánto sale el tornillo 6x50 y hay stock?"

Turn 1 → LLM decides to call get_price("tornillo 6x50")
       → Sheets returns "$15 por unidad"
       → tool_result appended

Turn 2 → LLM decides to call get_stock("tornillo 6x50")
       → Sheets returns "850 unidades en stock"
       → tool_result appended

Turn 3 → LLM produces final text:
         "El tornillo 6x50 sale $15 por unidad y tenemos 850 en stock 🔩"

Total: 3 LLM calls, 2 tool calls, 1 reply
```

---

## 6. Background Persistence

Supabase writes never block the user response. They run after the reply is already sent.

```mermaid
sequenceDiagram
    participant Handler as handler.py
    participant WhatsApp as WhatsApp API
    participant BG as BackgroundTask
    participant Supabase as Supabase

    Handler->>WhatsApp: send_message(user_phone, reply)
    WhatsApp-->>Handler: 200 OK
    Note over Handler: Response is returned to Meta here
    Handler->>Handler: save_history to Redis (sync)
    Handler->>Handler: release lock (sync)
    Handler-)BG: schedule background task (non-blocking)

    Note over BG,Supabase: Runs after response is fully sent
    BG->>Supabase: UPSERT conversations\n(client_id, user_phone)\nSET last_message_at = now()
    Supabase-->>BG: conversation_id

    BG->>Supabase: INSERT messages\n(user message, role=user)\n(assistant message, role=assistant,\nlatency_ms, prompt_version)

    alt Any exception
        BG->>BG: log error\nreturn silently\n(never raises)
    end
```

**What gets logged per conversation turn:**

```
conversations row:
  client_id, user_phone, last_message_at → updated on every message

messages rows (2 inserted per turn):
  role=user,      content="cuánto sale el tornillo?"  latency_ms=null
  role=assistant, content="El tornillo sale $15...",   latency_ms=1843, prompt_version=1
```

---

## 7. Full System Map

How all components relate to each other at runtime.

```mermaid
flowchart TD
    subgraph External
        WA["👤 WhatsApp User"]
        META["Meta Cloud API"]
        SHEETS["Google Sheets\nper-client product data"]
    end

    subgraph Railway ["Railway - Single Deployment"]
        subgraph FastAPI
            ROUTER["webhook/router.py\nGET + POST /webhook"]
            HANDLER["webhook/handler.py\nrequest lifecycle"]
        end

        subgraph Agent
            GRAPH["agent/graph.py\nLangGraph loop"]
            TOOLS["agent/tools.py\ntool handlers"]
            PROMPTS["agent/prompts.py\nprompt builder"]
        end

        subgraph Services
            CSVC["clients/service.py\nclient lookup"]
            CONVSVC["conversations/service.py\nlog to Supabase"]
        end

        subgraph Integrations
            WACLIENT["integrations/whatsapp.py\nsend_message"]
            SHEETSCLIENT["integrations/sheets.py\nread products"]
        end
    end

    subgraph Upstash ["Upstash Redis"]
        DEDUP["dedup:{message_id}\nTTL 60s"]
        LOCK["lock:{client_id}:{phone}\nTTL 10s"]
        HIST["history:{client_id}:{phone}\nTTL 24h"]
        CACHE["config:{phone}\nTTL 5min"]
    end

    subgraph Supabase ["Supabase Postgres"]
        CLIENTS_T["clients table"]
        CONV_T["conversations table"]
        MSG_T["messages table"]
    end

    subgraph Anthropic
        HAIKU["Claude Haiku"]
    end

    WA --> META --> ROUTER --> HANDLER

    HANDLER --> DEDUP
    HANDLER --> CACHE
    CACHE -.->|miss| CLIENTS_T
    HANDLER --> LOCK
    HANDLER --> HIST

    HANDLER --> GRAPH
    GRAPH --> PROMPTS
    GRAPH --> TOOLS
    GRAPH --> HAIKU
    TOOLS --> SHEETSCLIENT --> SHEETS

    HANDLER --> WACLIENT --> META --> WA

    HANDLER -.->|background| CONVSVC
    CONVSVC -.-> CONV_T
    CONVSVC -.-> MSG_T

    CSVC --> CLIENTS_T
    CSVC --> CACHE
```

--- 

## Component Responsibility Summary

| File | Responsibility | Touches |
|---|---|---|
| `webhook/router.py` | HTTP layer only — routes, verification, always-200 | FastAPI |
| `webhook/handler.py` | Full request lifecycle orchestration | Everything |
| `agent/graph.py` | LangGraph loop — LLM calls + tool dispatch | Anthropic, tools |
| `agent/tools.py` | Tool definitions + handlers | Sheets |
| `agent/prompts.py` | Build final system prompt string | ClientConfig |
| `clients/service.py` | Client lookup with cache | Supabase, Redis |
| `conversations/service.py` | Async Supabase logging | Supabase |
| `context/redis.py` | History, lock, dedup | Redis |
| `integrations/sheets.py` | Read product data | Google Sheets |
| `integrations/whatsapp.py` | Send messages | Meta API |
| `dependencies.py` | FastAPI DI wiring | All clients |
| `config.py` | Env var validation — crash on missing | — |