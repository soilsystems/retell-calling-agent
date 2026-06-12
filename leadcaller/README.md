# LeadCaller

LeadCaller is a production-ready AI lead qualification system built with FastAPI, async SQLAlchemy, Supabase Postgres, Retell AI outbound/inbound calls, Zoho CRM sync, and WhatsApp follow-ups via Exotel.

## Features

- HMAC-SHA256 verified Zoho and Retell webhooks with constant-time comparison.
- Idempotent webhook processing.
- Indian mobile validation for Zoho leads.
- Business-hour aware scheduling for IST, Monday through Saturday, 9:00 to 19:00.
- Retell outbound call creation with dynamic variables.
- Inbound call handling — matches existing lead or creates new one.
- Per-call language override (English / Hindi / Kannada) via Retell agent override.
- Retry scheduling for no-answer, busy, and failed calls.
- Zoho CRM lead update and follow-up task creation.
- Post-call WhatsApp brochure delivery via Exotel (Meta fallback).
- Race-safe dedup for double-send prevention.
- Alembic migration with Postgres enums, indexes, JSONB, RLS policies, and campaign metrics support.
- Docker and docker-compose for local development.
- Operations dashboard deployed on Vercel.

## Starting the server

From the repo root, run:

```bash
./start-dev.sh
```

This starts the FastAPI backend and the ngrok tunnel in one command. When both are up, it prints:

```
✓ Backend running on http://localhost:8000
✓ Tunnel running at https://dweller-tinkling-mutiny.ngrok-free.dev

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Dashboard → https://leadcaller-dashboard.vercel.app
  Backend   → https://dweller-tinkling-mutiny.ngrok-free.dev
  Local     → http://localhost:8000
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Press `Ctrl+C` to stop everything.

The dashboard at **https://leadcaller-dashboard.vercel.app** is publicly accessible — anyone on the team with the link can open it while the server is running. No login required.

**Requirements before running:**
- Python virtualenv at `leadcaller/backend/.venv` (run `python -m venv .venv && pip install -r requirements.txt` once)
- `leadcaller/backend/.env` populated (copy from `.env.example`)
- ngrok installed and authenticated (`ngrok config add-authtoken <token>`)

## Manual start (alternative)

```bash
# Terminal 1 — backend
cd leadcaller/backend && source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — ngrok
ngrok http --domain=dweller-tinkling-mutiny.ngrok-free.dev 8000
```

The API will be available at `http://localhost:8000`.

## Database Migration

```bash
docker compose run --rm app alembic upgrade head
```

## Webhook Endpoints

- `POST /webhooks/zoho/new-lead`
- `POST /webhooks/retell/call-completed`
- `GET /health`

## Tests

```bash
pytest
```

The test suite uses `pytest`, `pytest-asyncio`, `httpx.AsyncClient`, and `respx` for HTTP mocking.

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

## Meta Ads Integration

Meta lead forms should flow into Zoho CRM through Zoho's native Meta Ads integration. No FastAPI webhook is required for Meta directly.

1. In Zoho CRM, go to Setup -> Marketplace -> Meta Ads.
2. Click Connect and authorize the Facebook Business account for Soil Systems.
3. Select the Facebook or Instagram ad account used for the campaign.
4. Map form fields:
   - `full_name` -> Last Name / Full Name
   - `phone_number` -> Mobile
   - `email` -> Email
   - `city` -> City
   - `campaign_name` -> Campaign Name
   - `ad_name` -> Lead Source = `Meta Ads`
   - `platform` -> Description or a custom platform field
5. Configure the existing Zoho workflow so newly-created Zoho leads call `POST /webhooks/zoho/new-lead`.

If `language_preference` is missing or blank in the Zoho webhook payload, LeadCaller defaults it to `english` before scheduling the AI call.

## Scheduler

LeadCaller uses APScheduler's `AsyncIOScheduler` in the FastAPI lifespan. When `SCHEDULER_ENABLED=true`, the app runs `run_scheduled_calls()` every minute and triggers up to 10 pending `call_jobs` whose `scheduled_at` is due.

Business hours are Monday through Saturday, 09:00-19:00 IST. New lead calls trigger immediately during business hours. Outside business hours, the call job remains pending and is moved to the next business slot.

Set `SCHEDULER_ENABLED=false` in local development or tests when automatic call triggering should be disabled.

## Inbound Calls

Retell answers inbound calls on the registered Retell phone number and connects them to the assigned LeadQualifier agent. FastAPI handles the same completion webhook at `POST /webhooks/retell/call-completed`.

For inbound completions, LeadCaller matches the caller by phone number. If no lead exists, it creates an `Unknown` lead with source `Inbound Call`, creates the corresponding Zoho lead, and logs the call attempt with `direction=inbound`.

Zoho sync includes callback and direction fields:
- `AI_Callback_Scheduled`
- `AI_Callback_Time`
- `AI_Call_Direction`
- `AI_Last_Call_Trigger_Reason`
