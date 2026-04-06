 SOLID + Clean Code Refactoring Plan — app/                                                                                                                 
                                                                                                                                                            
 Context                                                                                                                                                    
                                                                                                                                                            
 The codebase is a WhatsApp AI receptionist for small businesses. It works, but onboarding a new client vertical (padel, dentist, generic retail) requires  
 reading the code backwards to find all the places to touch: tools.py, clients/models.py, hardcoded string literals, and copy-pasted date/timezone          
 constants across four files. The goal is crystal-clear traceability: every tool's dependencies and intent are discoverable by following the code forward,
 not backward.
                                                                                                                                                            
 External API surface (Redis keys, Supabase schema, WhatsApp/MP endpoints) is never touched. The app must run correctly after each phase.                   
                                                                                                                                                            
 ---
 Phase 1 — Foundation: Shared Constants, Config Hardening, Model Correctness

 Fixes: G5 (DRY), G25 (named constants), mutable defaults, weak types, fail-fast startup.

 1.1 New file: app/integrations/argentina.py

 Canonical home for locale constants currently copy-pasted in calendar.py, calendar_mock.py, padel_calendar.py, padel_tools.py:

 ART = timezone(timedelta(hours=-3))
 DAYS_ES: list[str] = ["lunes", "martes", ...]
 MONTHS_ES: list[str] = ["", "enero", "febrero", ...]

 def fmt_datetime(dt: datetime) -> str   # "martes 18 de marzo a las 10:00"
 def fmt_date(d: date) -> str             # "martes 18 de marzo de 2026"
 def now_art() -> datetime

 Delete local copies in those four files; import from here.

 1.2 New file: app/integrations/calendar_config.py

 DEFAULT_SLOT_MINUTES: int = 30
 DEFAULT_WORK_START: int = 10
 DEFAULT_WORK_END: int = 18
 DEFAULT_WORK_DAYS: frozenset[int] = frozenset({0, 1, 2, 3, 4})
 SLOT_LOCK_TTL: int = 300
 APPOINTMENT_TTL: int = 60 * 60 * 24 * 30

 calendar.py and calendar_mock.py import these. padel_calendar.py imports only the TTL constants (padel uses 60-min slots, different semantic).

 1.3 Fix app/conversations/models.py

 - role: str → role: Literal["user", "assistant", "tool"]
 - messages: list[Message] = [] → messages: list[Message] = Field(default_factory=list)

 1.4 Harden app/config.py

 - Wrap secrets in SecretStr: whatsapp_access_token, whatsapp_verify_token, supabase_service_key, UPSTASH_REDIS_REST_TOKEN, anthropic_api_key,
 openai_api_key
 - Add @model_validator(mode="after") that raises ValueError if UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN are empty — fail fast at startup
 - Callers use .get_secret_value(): context/redis.py, dependencies.py, integrations/whatsapp.py, integrations/sheets.py

 1.5 Fix app/clients/models.py

 - tools_enabled: list[str] → add Field(default_factory=list)
 - Stub @field_validator("tools_enabled") (completes in Phase 2 once registry exists)

 Verification: python -c "from app.config import settings" succeeds; mutable list isolation test; from app.integrations.argentina import ART works.

 ---
 Phase 2 — Tool Registry: Single Source of Truth for Tool Names

 Fixes: G5 (DRY), G16 (obscured intent), G25 (named constants). Eliminates scattered string literals.

 2.1 New file: app/agent/registry.py

 from enum import StrEnum

 class ToolName(StrEnum):
     GET_PRICE              = "get_price"
     GET_STOCK              = "get_stock"
     GET_ALL_PRODUCTS       = "get_all_products"
     GET_PRODUCTS_BY_CAT    = "get_products_by_category"
     GET_HOURS              = "get_hours"
     GENERATE_PAYMENT_LINK  = "generate_payment_link"
     GET_TREATMENT_INFO     = "get_treatment_info"
     GET_PRICES_DENTIST     = "get_prices"
     GET_INSURANCES         = "get_insurances"
     GET_CURRENT_DATE_HOUR  = "get_current_date_hour"
     CHECK_AVAILABILITY     = "check_availability"
     BOOK_APPOINTMENT       = "book_appointment"
     GET_APPOINTMENT        = "get_appointment"
     CANCEL_APPOINTMENT     = "cancel_appointment"
     RESCHEDULE_APPOINTMENT = "reschedule_appointment"
     GET_AVAILABILITY_PADEL = "get_availability"
     CREATE_BOOKING_PADEL   = "create_booking"
     CANCEL_BOOKING_PADEL   = "cancel_booking"
     GENERATE_PADEL_PAYMENT = "generate_padel_payment_link"

 CALENDAR_TOOLS: frozenset[ToolName] = frozenset({...})
 DENTIST_INFO_TOOLS: frozenset[ToolName] = frozenset({...})
 PADEL_TOOLS: frozenset[ToolName] = frozenset({...})
 KNOWN_TOOL_NAMES: frozenset[str] = frozenset(t.value for t in ToolName)

 2.2 Update app/agent/tools.py

 Replace the three inline set literals for calendar/dentist/padel detection with imports from registry.py:

 enabled = frozenset(config.tools_enabled)
 if enabled & CALENDAR_TOOLS and redis is not None: ...
 if enabled & DENTIST_INFO_TOOLS and config.prices_sheet_id: ...
 if enabled & PADEL_TOOLS and redis is not None: ...

 2.3 Complete ClientConfig validator (clients/models.py)

 from app.agent.registry import KNOWN_TOOL_NAMES

 @field_validator("tools_enabled")
 @classmethod
 def validate_tool_names(cls, v: list[str]) -> list[str]:
     unknown = set(v) - KNOWN_TOOL_NAMES
     if unknown:
         raise ValueError(f"Unknown tool names: {unknown}")
     return v

 Verification: python -c "from app.agent.registry import KNOWN_TOOL_NAMES; print(len(KNOWN_TOOL_NAMES))" prints ~19;
 ClientConfig(tools_enabled=["invalid"]) raises ValidationError.

 ---
 Phase 3 — Integration Layer: Per-Client Config + Consistent Error Handling

 Fixes: G5 (DRY), G30 (SRP), G36 (Law of Demeter). Business constants flow from config, not hardcoded.

 3.1 Add scheduling fields to ClientConfig

 New fields with Supabase-safe defaults (no migration needed; missing columns → default values via Pydantic):

 slot_minutes: int = 30
 work_start_hour: int = 10
 work_end_hour: int = 18
 work_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
 sheet_tab_treatments: str = "Tratamientos"
 sheet_tab_insurances: str = "Obras Sociales"
 sheet_tab_products: str = "productos"

 3.2 Thread config into GoogleCalendarClient and CalendarMock

 Both constructors accept slot_minutes, work_start, work_end, work_days. The module-level constants become constructor defaults only. _next_candidates()
 uses the instance parameters.

 In build_tools(), pass config.slot_minutes, config.work_start_hour, etc. when constructing calendar clients.

 3.3 Thread sheet tab names into dentist_sheets.py

 Public functions accept tab_treatments and tab_insurances kwargs with current hardcoded names as defaults. build_tools() passes config.sheet_tab_*.

 3.4 Expose Redis via property in ConversationContext

 handler.py:295 accesses conversation_context._redis (Law of Demeter violation). Add public redis property returning self._redis_client. Handler uses
 conversation_context.redis.

 Verification: Create ClientConfig with slot_minutes=60; assert GoogleCalendarClient uses it in check_availability. grep "_redis" app/webhook/handler.py
 returns nothing.

 ---
 Phase 4 — Handler Decomposition: Single Responsibility per Function

 Fixes: G30 (SRP), G16 (obscured intent). handle_message() drops from 215 lines to ~20.

 4.1 New file: app/webhook/payload_parser.py

 Move _extract_message_fields() here. Define:

 @dataclass
 class ParsedMessage:
     message_id: str
     user_phone: str
     message_text: Optional[str]
     inbound_number: str
     media_id: Optional[str]

 def parse_payload(payload: dict) -> Optional[ParsedMessage]

 4.2 Split handle_message() into named steps

 Extract these private async functions (each 10–25 lines, single responsibility):

 async def _resolve_client(inbound_number, get_client_by_phone) -> Optional[ClientConfig]
 async def _resolve_text(parsed, transcriber, whatsapp) -> Optional[str]
 async def _wait_for_debounce(context, client_id, user_phone) -> None
 async def _run_conversation_turn(context, config, user_phone, sheets, redis, user_text) -> TurnResult

 TurnResult = @dataclass with reply: str, latency_ms: int, messages: list[Message].

 handle_message() becomes an orchestrator of ~20 lines calling each step in sequence.

 4.3 Fix the late import of run_agent

 Move from app.agent.graph import run_agent to top-level in handler.py. The circular import that caused the late-import workaround is eliminated once
 registry.py is the intermediary.

 4.4 Remove module-level singleton from context/redis.py

 Delete lines 192–213 (the _redis_client = Redis(...), conversation_context = ConversationContext(...) singleton, and all shim functions). All callers use
 the FastAPI DI get_conversation_context from dependencies.py. Module import no longer tries to connect to Redis.

 Verification: python -c "from app.webhook.handler import handle_message" completes with no Redis connection. Each extracted step has a focused unit test
 in tests/test_handler_steps.py.

 ---
 Phase 5 — Vertical Plugin Pattern: One File per Business Vertical

 Fixes: G23 (polymorphism over if/else), OCP (open for extension, closed for modification). New verticals require touching zero existing files.

 5.1 New file: app/agent/base_toolset.py

 from typing import Protocol

 class ToolsetProvider(Protocol):
     name: str
     required_tools: frozenset[str]

     def is_applicable(self, config: ClientConfig) -> bool: ...
     def build(self, config: ClientConfig, **deps) -> list[dict]: ...
     # deps: sheets, redis, user_phone, client_id

 5.2 New directory app/agent/verticals/ — one file per vertical

 Each file wraps the existing factory functions (which do not change) inside a class:

 ┌───────────────────────────────┬────────────────────────┬─────────────────────────────────────────────────────────┐
 │             File              │         Class          │                 is_applicable condition                 │
 ├───────────────────────────────┼────────────────────────┼─────────────────────────────────────────────────────────┤
 │ verticals/retail.py           │ RetailToolset          │ config.sheet_id and enabled & retail_tool_names         │
 ├───────────────────────────────┼────────────────────────┼─────────────────────────────────────────────────────────┤
 │ verticals/calendar_booking.py │ CalendarBookingToolset │ config.calendar_id and enabled & CALENDAR_TOOLS         │
 ├───────────────────────────────┼────────────────────────┼─────────────────────────────────────────────────────────┤
 │ verticals/dentist.py          │ DentistToolset         │ config.prices_sheet_id and enabled & DENTIST_INFO_TOOLS │
 ├───────────────────────────────┼────────────────────────┼─────────────────────────────────────────────────────────┤
 │ verticals/padel.py            │ PadelToolset           │ config.calendar_id and enabled & PADEL_TOOLS and redis  │
 └───────────────────────────────┴────────────────────────┴─────────────────────────────────────────────────────────┘

 5.3 Rewrite build_tools() as a registry loop

 _TOOLSETS: list[ToolsetProvider] = [
     RetailToolset(), CalendarBookingToolset(), DentistToolset(), PadelToolset(),
 ]

 def build_tools(config, sheets, redis=None, user_phone="", client_id="") -> list[dict]:
     deps = {"sheets": sheets, "redis": redis, "user_phone": user_phone, "client_id": client_id}
     all_tools: dict[str, dict] = {}
     for toolset in _TOOLSETS:
         if toolset.is_applicable(config):
             for tool in toolset.build(config, **deps):
                 all_tools[tool["definition"]["name"]] = tool
     return [all_tools[n] for n in config.tools_enabled if n in all_tools]

 5.4 Standardize all tool handlers to async

 All tool handler functions become async. Sync implementations become:

 async def handler(product: str) -> str:
     return _sync_logic(product)

 Delete inspect.iscoroutinefunction() check in graph.py:150. Every call becomes await handler(**args).

 New vertical acceptance test (no existing files touched):
 1. Add GYM_SCHEDULE = "gym_schedule" to ToolName in registry.py
 2. Create app/agent/verticals/gym.py with GymToolset
 3. Add GymToolset() to _TOOLSETS
 4. build_tools(ClientConfig(tools_enabled=["gym_schedule"]), ...) returns one tool

 ---
 File Change Summary

 ┌───────┬────────────────────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────────────────┐
 │ Phase │                             Modified files                             │                             New files                              │
 ├───────┼────────────────────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────┤
 │ 1     │ calendar.py, calendar_mock.py, padel_calendar.py, padel_tools.py,      │ integrations/argentina.py, integrations/calendar_config.py         │
 │       │ conversations/models.py, config.py, clients/models.py                  │                                                                    │
 ├───────┼────────────────────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────┤
 │ 2     │ agent/tools.py, clients/models.py                                      │ agent/registry.py                                                  │
 ├───────┼────────────────────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────┤
 │       │ clients/models.py, integrations/calendar.py,                           │                                                                    │
 │ 3     │ integrations/calendar_mock.py, integrations/dentist_sheets.py,         │ —                                                                  │
 │       │ context/redis.py, webhook/handler.py                                   │                                                                    │
 ├───────┼────────────────────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────┤
 │ 4     │ webhook/handler.py, context/redis.py, webhook/router.py                │ webhook/payload_parser.py, tests/test_handler_steps.py             │
 ├───────┼────────────────────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────────────────┤
 │ 5     │ agent/tools.py, agent/graph.py                                         │ agent/base_toolset.py,                                             │
 │       │                                                                        │ agent/verticals/{retail,calendar_booking,dentist,padel}.py         │
 └───────┴────────────────────────────────────────────────────────────────────────┴────────────────────────────────────────────────────────────────────┘

 What Is Never Changed

 - LangGraph graph topology in graph.py (node wiring, _AgentState, _to_openai_tool)
 - All Redis key patterns (history:, lock:, buffer:, dedup:, slot_lock:, appt:, mp_payment:, padel:booking:)
 - Supabase schema (new ClientConfig fields default gracefully on missing columns)
 - WhatsApp + MP endpoint paths
 - definition/handler dict shape consumed by graph.py
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌