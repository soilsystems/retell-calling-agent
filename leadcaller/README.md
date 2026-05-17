# LeadCaller

LeadCaller is a production-ready AI lead qualification backend built with FastAPI, async SQLAlchemy, Supabase Postgres, Retell AI outbound calls, and Zoho CRM sync.

## Features

- HMAC-SHA256 verified Zoho and Retell webhooks with constant-time comparison.
- Idempotent webhook processing.
- Indian mobile validation for Zoho leads.
- Business-hour aware scheduling for IST, Monday through Saturday, 9:00 to 19:00.
- Retell outbound call creation with dynamic variables.
- Retry scheduling for no-answer, busy, and failed calls.
- Zoho CRM lead update and follow-up task creation.
- Alembic migration with Postgres enums, indexes, JSONB, RLS policies, and campaign metrics support.
- Docker and docker-compose for local development.

## Quick Start

```bash
cp .env.example .env
docker compose up --build
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
