# AI Receptionist
 
An open-source, WhatsApp-native AI agent that handles customer-facing operations for small businesses — answering product questions, checking stock, booking appointments, and generating MercadoPago payment links in real time.
 
Built with [LangGraph](https://github.com/langchain-ai/langgraph), FastAPI, and the WhatsApp Business API. Designed to work out of the box for businesses that already manage their data in Google Sheets.
 
---
 
## What it does
 
Customers message your business on WhatsApp. The agent understands natural language, calls the right tools, and responds — without any human in the loop.
 
**Supported capabilities (configurable per business):**
 
- Product catalog, stock queries, and pricing via Google Sheets
- Appointment booking with availability checking and confirmation
- MercadoPago payment link generation for reservations or purchases
- Order creation for custom/pre-order products
- Business hours lookups
- Multi-location support (e.g. a bakery with 3 branches)
**Live client profiles included** (`prompts_context.md`):
 
| Business | Tools used |
|---|---|
| Hardware store (Ferretería) | `get_price`, `get_stock`, `get_all_products`, `generate_payment_link` |
| Dental clinic | `check_availability`, `book_appointment`, `generate_payment_link`, `get_insurances` |
| Bakery (multi-location) | `get_stock`, `get_price`, `create_order`, `generate_payment_link` |
| Padel club | `get_availability`, `create_booking`, `cancel_booking`, `generate_padel_payment_link` |
 
Each profile is a standalone system prompt + tool list. Adding a new business type means writing a new profile — no code changes required.
 
---
 
## Architecture
 
```
WhatsApp (Meta Cloud API)
        │
        ▼
   FastAPI webhook
        │
        ▼
  LangGraph agent ──── Tool dispatcher
        │                    │
        │         ┌──────────┼──────────┐
        │         ▼          ▼          ▼
        │   Google Sheets  Supabase  MercadoPago
        │   (inventory,    (bookings, (payment
        │    pricing,       orders)    links)
        │    catalog)
        │
        ▼
  Upstash Redis (conversation memory per phone number)
        │
        ▼
  WhatsApp reply
```
 
**Stack:**
 
- **Runtime:** Python 3.11, FastAPI, Uvicorn
- **Agent:** LangGraph (tool-calling loop over OpenAI / Anthropic models)
- **Data:** Google Sheets via service account (no database needed for basic setups)
- **Persistence:** Supabase (bookings, orders), Upstash Redis (session state)
- **Observability:** Logfire (FastAPI + OpenAI + Redis tracing), Sentry
- **Deployment:** Railway (`railway.toml` + `Procfile` included)
---
 
## Quick start
 
### Prerequisites
 
- Python 3.11+
- A WhatsApp Business account with Meta Cloud API access
- A Google Cloud service account with Sheets access
- Supabase project (free tier works)
- Upstash Redis instance (free tier works)
- OpenAI or Anthropic API key
### 1. Clone and install
 
```bash
git clone https://github.com/WencesCapolo/ai-recepcionist.git
cd ai-recepcionist
pip install -e .
```
 
### 2. Configure environment
 
```bash
cp .env.example .env
# Fill in your credentials — see Environment Variables below
```
 
### 3. Run locally
 
```bash
uvicorn app.main:app --reload
```
 
Expose your local server with [ngrok](https://ngrok.com/) and register the webhook URL in Meta's Developer Console.
 
### 4. Run tests
 
```bash
pytest
```
 
---
 
## Environment variables
 
| Variable | Description |
|---|---|
| `WHATSAPP_VERIFY_TOKEN` | Token for Meta webhook verification |
| `WHATSAPP_ACCESS_TOKEN` | Meta Cloud API bearer token |
| `WHATSAPP_PHONE_NUMBER_ID` | Your WhatsApp business phone number ID |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis REST token |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude models) |
| `OPENAI_API_KEY` | OpenAI API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Base64-encoded service account JSON |
| `SENTRY_DSN` | Sentry DSN for error tracking |
| `LOGFIRE_TOKEN` | Logfire token for observability |
 
See `.env.example` for the full list.
 
---
 
## Configuring a business
 
Each business is defined by a **system prompt** and a **tool list**. See `prompts_context.md` for working examples across four industries.
 
The agent reads product data from a Google Sheet. The expected schema depends on the tools used — the hardware store example uses columns `producto`, `categoria`, `precio`, `stock`, `unidad`. Matching your sheet to the tool definitions is the main configuration step.
 
### Adding a new business type
 
1. Write a system prompt defining the agent's persona, tone, and behavioral rules.
2. Declare which tools are available for that business.
3. Set the `sheet_id` pointing to the business's Google Sheet.
4. Deploy and register the webhook for that business's WhatsApp number.
Multi-tenant support (multiple businesses on one deployment) is on the roadmap.
 
---
 
## Project structure
 
```
ai-recepcionist/
├── app/                  # FastAPI application and agent logic
├── docs/                 # Architecture and design notes
├── tests/                # pytest test suite
├── .agents/skills/       # Agent skill definitions
├── prompts_context.md    # Business profiles (prompts + tool configs + mock data)
├── pyproject.toml        # Dependencies and build config
├── railway.toml          # Railway deployment config
└── Procfile              # Process declaration for deployment
```
 
---
 
## Deployment
 
The project is pre-configured for [Railway](https://railway.app/). Push to your Railway project and set environment variables via the Railway dashboard.
 
```bash
railway up
```
 
For other platforms, the app is a standard ASGI app. Any platform that can run `uvicorn app.main:app` will work.
 
---
 
## Roadmap
 
- [ ] Multi-tenant support (one deployment, multiple businesses)
- [ ] Web-based configuration panel for non-technical users
- [ ] Invoice generation (AFIP/ARCA integration for Argentine businesses)
- [ ] Telegram and Instagram DM channels
- [ ] Webhook signature verification hardening
---
 
## Contributing
 
Issues and pull requests are welcome. If you're adapting this for a new business vertical or country-specific payment provider, please open an issue first to discuss the approach.
 
---
 
## License
 
MIT
