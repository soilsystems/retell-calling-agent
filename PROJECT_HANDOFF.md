# LeadCaller — Project Handoff

_Last updated: 2026-06-28_

LeadCaller is an AI lead-qualification system for **Soil Systems** (Woods & Spices farmland project). It pulls leads from Zoho CRM (fed by Meta/Instagram ads), places AI voice calls via **Retell AI** bridged through **Exotel**, sends post-call WhatsApp follow-ups, schedules retries/callbacks, and exposes an operations dashboard.

---

## 1. Architecture

```
 Meta/Instagram Ads ──> Zoho CRM ──(pull, every 3 min)──┐
                                                        ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  BACKEND (FastAPI, Railway)  leadcaller-backend-production...app   │
   │   • APScheduler: run_scheduled_calls (1m), zoho sync (3m),         │
   │     idle-conn sweeper (5m)                                         │
   │   • Outbound: Exotel /Calls/connect bridges lead <-> Retell SIP    │
   │   • Webhooks: /retell/inbound, /retell/call-completed,             │
   │     /exotel/status, /whatsapp/*, /webhooks/meta/*                  │
   │   • Post-call: Zoho sync + WhatsApp brochure                       │
   └──────────────┬─────────────────────────────┬─────────────────────┘
                  │ Supabase Postgres            │ Exotel WhatsApp API
                  ▼ (session pooler :5432)       ▼ (woods_and_spices template)
   ┌──────────────────────────┐      WhatsApp brochure / feedback to lead
   │ FRONTEND (Vite/React)     │
   │ leadcaller-dashboard      │  reads /admin/* (direct CORS to Railway)
   │ .vercel.app               │
   └──────────────────────────┘
```

**Outbound call flow (the key, non-obvious part):**
1. Backend calls Exotel `/Calls/connect`. By default `From = lead`, `To = Retell SIP` (`+918046376848`).
2. Exotel dials the lead (Leg 1); on answer it dials Retell SIP (Leg 2).
3. Retell receives this as an **inbound** call and hits `POST /webhooks/retell/inbound`.
4. That handler detects it's an **outbound bridge** (via an in-memory cache keyed by phone/Call-SID, or a recent `exotel_connect_call` CrmSyncLog) and returns dynamic variables + `begin_message` so the AI speaks the outbound script with the lead's name.
5. On hang-up Retell fires `POST /webhooks/retell/call-completed` → Zoho sync + WhatsApp template.

**No-answer detection:** for un-answered outbound calls Retell is never bridged, so **Exotel's status callback** (`/webhooks/exotel/status`, status `busy/no_answer/failed`) is the only signal — it records the attempt and schedules the twice-daily retry.

**Inbound calls:** customer dials `+918046376848` → Retell answers → `/retell/inbound` returns the inbound greeting. Real caller phone is resolved post-call from Exotel's Calls API (the SIP `from_number` is our own ExoPhone).

---

## 2. Folder structure

```
retell-calling-agent/
├── PROJECT_HANDOFF.md          ← this file
├── start-dev.sh                ← local: starts backend + ngrok
└── leadcaller/
    ├── README.md
    ├── docker-compose.yml
    ├── backend/
    │   ├── Dockerfile          ← runs `alembic upgrade head && uvicorn`
    │   ├── railway.json        ← Railway deploy config (healthcheck /health)
    │   ├── requirements.txt
    │   ├── alembic/versions/   ← 001..009 migrations
    │   └── app/
    │       ├── main.py         ← FastAPI app, CORS, APScheduler jobs
    │       ├── config.py       ← all settings / env vars (pydantic-settings)
    │       ├── database.py     ← async engine (pooled, session pooler :5432)
    │       ├── call_scripts.py ← begin_message / script templates
    │       ├── models/         ← SQLAlchemy: lead, call_job, call_attempt,
    │       │                      whatsapp_message, whatsapp_log, followup,
    │       │                      crm_sync_log, webhook_event, zoho_token, enums
    │       ├── schemas/        ← pydantic: lead_schema, retell_schema
    │       ├── routers/        ← admin, webhooks, whatsapp, meta_webhook, debug
    │       ├── services/       ← retell_service, exotel_service,
    │       │                      exotel_whatsapp_service, whatsapp_service,
    │       │                      zoho_service, meta_service, lead_service
    │       └── utils/          ← business_hours, phone, security
    └── frontend/
        ├── vercel.json         ← framework=vite + /admin /whatsapp rewrites→Railway
        ├── package.json
        └── src/
            ├── main.tsx        ← entire dashboard (single file, ~1900 lines)
            └── styles.css
```

---

## 3. Technologies

| Layer | Stack |
|---|---|
| Backend | Python 3.11, FastAPI 0.111, SQLAlchemy 2.0 (async), asyncpg, Alembic, APScheduler, Pydantic v2, httpx |
| DB | Supabase Postgres (connect via **session pooler, port 5432**; pooled connections) |
| Frontend | Vite 7, React 19, TypeScript, lucide-react, retell-client-js-sdk (single-file `main.tsx`) |
| Voice | Retell AI (agent `agent_f97c51d6f6a367f30e57df6f99`, LLM `llm_74885ab2c053c5f36aadc631f828`, published **v13**) |
| Telephony | Exotel (ExoPhone `+918046376848`, two-leg `/Calls/connect` bridge) |
| WhatsApp | Exotel WhatsApp v2 API (template `woods_and_spices`); Meta direct = fallback |
| CRM | Zoho CRM (India DC, `zohoapis.in`) |
| Hosting | Backend → Railway; Frontend → Vercel; tests → pytest |

---

## 4. Implementation status

**Production, live, working end-to-end.** Backend on Railway, frontend on Vercel, DB on Supabase. 90 backend tests pass. Laptop not required (fully cloud-hosted). All webhooks point at Railway.

- Live backend: `https://leadcaller-backend-production.up.railway.app`
- Live dashboard: `https://leadcaller-dashboard.vercel.app`

---

## 5. Features completed

**Calling**
- Outbound AI calls via Exotel→Retell bridge (greets lead by name, single intro, no lead-source mention).
- Inbound AI calls (different greeting/script); real caller resolved from Exotel Calls API (time-correlated, ExoPhone excluded, retries past Exotel API lag).
- Single-language per call (`agent_override.language` from lead preference: en-IN/hi-IN/kn-IN) — fixes multilingual TTS breaking on Kannada.
- **No-answer retries: twice daily (10am & 2pm IST) for 5 days (up to 10 attempts) until pickup** — driven by the Exotel status callback.
- Callback scheduling from call structured-data, with a loop guard (max 3 auto-callbacks / 2h).
- "Call a number" — place an AI call to any typed number from the dashboard.

**WhatsApp**
- Post-call brochure template (`woods_and_spices`) sent once per call, **with required Document header (brochure PDF)**.
- Also sent on first missed (no-answer) attempt.
- Two-way WhatsApp chat (send/receive text, image, video, audio, document, location) — `whatsapp_message` table + chat UI.
- Delivery receipts (DLR) wired to `/webhooks/whatsapp/status` for visibility.
- Visit-feedback message (plain text, configurable `WA_FEEDBACK_MESSAGE`) sent when a lead is marked Visited.

**Dashboard**
- Lead Activity ordered by most recent activity (GREATEST of last call / created / updated) — new leads + just-called leads both surface at top.
- Per-lead: Picked-up/Not-picked pill, call count, Visited checkbox (optimistic), Site-visit box, next scheduled call.
- **Callbacks page**: reason (requested vs no-pickup), tries so far, "picked up after X tries".
- 15s auto-refresh; resilient (one failing endpoint no longer blanks the view); Zoho sync moved to backend (no per-tab lock contention).
- One attempt row per connected call (Exotel recording merged onto the Retell attempt).

**Infra / data**
- Migration `009` added site-visit columns; Zoho sync runs on backend scheduler (serialized).
- Idle-in-transaction connection sweeper (every 5 min) + connection pooling (fixed cross-region 2.5s-per-request handshake).

---

## 6. Features pending / not done

- **Direct Retell SIP origination** (the real fix for the call-connect ringback / "speak-first timing"). Blocked on Exotel providing the **SIP trunk termination URI/hostname**. See Known Issues #1.
- **Meta Business Verification** — lifts the WhatsApp per-user / 250-conversations-per-day marketing cap (currently can throttle delivery to over-tested numbers).
- **Meta direct WhatsApp send** — phone is registered to Exotel's Cloud-API app; needs Exotel to deregister it. Exotel path works, so this is just a fallback.
- **`visit_feedback` as a template** — currently plain text, which only delivers inside the lead's 24h window. A template would deliver anytime (needs Meta approval).
- **Stale `in_progress` call-job sweeper** — flagged as a background task; jobs can get stuck `in_progress` if a trigger fails.
- **`call_followup` / `call_missed` distinct templates** — config keys exist but those templates aren't created in Exotel; all post-call uses `woods_and_spices`.
- Historical duplicate `exotel:` attempt rows from before the dedup fix are still in the DB (cleanup optional).

---

## 7. Important design decisions

1. **Outbound = Exotel two-leg bridge, not Retell direct outbound.** The Retell SIP outbound trunk lacked working auth creds, so we dial via Exotel and let Retell treat it as inbound. The `/retell/inbound` handler distinguishes outbound bridges from real inbound via an in-memory cache + recent-CrmSyncLog fallback.
2. **Inbound webhook must respond fast (<~2s)** or Retell plays "please wait". Genuine inbound takes the fast path (no heavy DB).
3. **Session pooler (:5432) + connection pool**, not transaction pooler (:6543) + NullPool. NullPool paid a ~2.5s connection handshake on every request (Railway US ↔ Supabase Tokyo); the transaction pooler breaks asyncpg prepared statements under pooling. Session pooler gives each connection a dedicated backend so pooling works.
4. **Zoho sync runs on the backend scheduler, not per dashboard refresh.** Per-tab 15s syncs caused `leads`-table lock contention → statement-timeout errors that broke webhooks. `sync_recent_zoho_leads` is serialized via an asyncio lock.
5. **Idle-in-transaction sweeper** because Supabase's transaction pooler ignores `idle_in_transaction_session_timeout`; leaked transactions otherwise pile up and hold locks.
6. **One template per call.** Retell `call_ended` + `call_analyzed` both fire; `send_post_call_template` is deduped via an in-memory claim set, and the legacy `soil_systems` send on the Exotel "completed" path was removed.
7. **`woods_and_spices` needs a Document header.** Meta drops it (`EX_TEMPLATE_PARAM_ERROR`) without the brochure PDF attached — verified via DLR.
8. **Dashboard talks to Railway directly** (CORS allowed for `*.vercel.app`). `vercel.json` rewrites are an alternate same-origin path.
9. **Retell agent version is never pinned** — the bridge hands Retell the agent ID and Retell runs whatever's published (currently v13). The `_register_retell_phone_call` path (and `RETELL_AGENT_VERSION`) is dead code.
10. **Do not reference lead source** (Instagram/Meta/Zoho) in any AI script or message — explicit business constraint.

---

## 8. Known issues

1. **Outbound ringback / speak-timing (ACTIVE, with Exotel).** Exotel's bridge gives an unavoidable trade-off: lead-first = clean greeting but ~6-8s ring-back (telecom + Leg-2); Retell-first (`PREWARM_RETELL_LEG`) = no ring-back but the bot greets before the person is on (it can't detect pickup). Attempted fix: `start_speaker=user` per-call override (commit `7286a948`) so the bot waits for the person — **awaiting a live test to confirm Retell honors the per-call override.** Retell's API rejects changing `start_speaker`/`begin_message` on the published LLM (the dashboard toggle would be the reliable fallback). **The only clean solution = Retell SIP origination, blocked on Exotel's termination URI.**
2. **WhatsApp delivery to over-tested numbers** can hit Meta's per-user marketing cap (`EX_RESTRICTED_BY_META`). Resolve via Meta Business Verification or test with fresh numbers.
3. **Visit feedback (plain text) only delivers in the 24h window** (`EX_REENGAGEMENT_ERROR` otherwise) — inherent WhatsApp policy; needs a template for anytime delivery.
4. **`migration 008`** was rewritten to NOT build the trgm index via `CREATE INDEX CONCURRENTLY` (it hung forever on the Supabase pooler and spawned lock-holding zombie backends). The index is a scale optimization, omitted at current size.

---

## 9. Current branch & recent changes

- **Branch:** `main` (work is committed directly to main; feature branches merged + deleted).
- **Latest commit:** `7286a948 feat(prewarm): make bot wait for the person (start_speaker=user) per call`
- **Recent commits (newest first):**
  - `7286a948` prewarm: start_speaker=user per call
  - `e2f9b102` script: agent introduces itself (no "am I speaking with…")
  - `cb6e1770` / `9fc80fa3` / `feb5450f` / `e6a760e0` prewarm leg-swap + wait-for-user iterations
  - `a6489033` one attempt row per connected call
  - `d4667c51` store Exotel recording URL on attempts
  - `0846fb0d` surface new leads (order by latest of call/created/updated)
  - `e6315e1a` visit feedback as plain text
  - `1325ac9b` attach brochure document header to woods_and_spices
  - `eb669bf1` whatsapp status_callback for delivery receipts
  - `27572018` just-called lead at top; one template per call
  - `8893cd49` Zoho sync → backend scheduler (lock-contention fix)
  - `37714209` Callbacks page
  - `ca9f0961` resilient dashboard refresh
  - `d81d11f2` / `8204323d` / `e9fc8d03` / `ba833595` dashboard: call count, retries as callbacks, activity ordering, optimistic Visited

### Files modified recently
`leadcaller/backend/app/`: `call_scripts.py`, `config.py`, `main.py`, `routers/admin.py`, `routers/webhooks.py`, `services/exotel_service.py`, `services/exotel_whatsapp_service.py`, `services/zoho_service.py`, `tests/test_retell_webhook.py`; `leadcaller/frontend/src/`: `main.tsx`, `styles.css`.

---

## 10. Environment variables

Set in `leadcaller/backend/.env` locally and in **Railway** for prod (`.env` is gitignored). Names only — values live in Railway/`.env`.

**Core:** `BASE_URL`, `ENVIRONMENT`, `LOG_LEVEL`, `SCHEDULER_ENABLED`, `DATABASE_URL` (Supabase; app rewrites `:6543`→`:5432`), `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `CORS_ALLOW_ORIGINS`, `CORS_ALLOW_ORIGIN_REGEX`.

**Retell:** `RETELL_API_KEY`, `RETELL_AGENT_ID`, `RETELL_INBOUND_AGENT_ID`, `RETELL_FROM_NUMBER`, `RETELL_WEBHOOK_SECRET`, `RETELL_AGENT_VERSION` (dead code), `RETELL_IMPORT_PHONE_NUMBER_ENDPOINT`.

**Exotel (voice):** `EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_SUBDOMAIN`, `EXOTEL_CALLER_ID`, `EXOTEL_PHONE_NUMBER`, `EXOTEL_CALL_TYPE`, `EXOTEL_STATUS_CALLBACK`, `EXOTEL_EXOML_URL`, `EXOTEL_TIMEZONE`, `EXOTEL_TRUNK_SID`, `EXOTEL_TERMINATION_URI`, `EXOTEL_SIP_AUTH_USERNAME`, `EXOTEL_SIP_AUTH_PASSWORD`, `EXOTEL_TRANSPORT`, `PREWARM_RETELL_LEG` (currently **True** for testing; set False to revert).

**Exotel WhatsApp / Meta:** `EXOTEL_WA_ACCOUNT_SID`, `EXOTEL_WA_API_KEY`, `EXOTEL_WA_API_TOKEN`, `EXOTEL_WA_SUBDOMAIN`, `EXOTEL_WA_PHONE_NUMBER`, `EXOTEL_WHATSAPP_NUMBER`, `EXOTEL_WHATSAPP_FROM_NUMBER`, `EXOTEL_WA_TEMPLATE_POST_CALL` (=`woods_and_spices`), `EXOTEL_WA_TEMPLATE_POST_CALL_LANG`, `EXOTEL_WA_TEMPLATE_POST_CALL_DOC_URL`, `EXOTEL_WA_TEMPLATE_POST_CALL_DOC_NAME`, `EXOTEL_WA_TEMPLATE_SOIL_SYSTEMS`, `EXOTEL_WA_TEMPLATE_COMPLETED`, `EXOTEL_WA_TEMPLATE_MISSED`, `WA_FEEDBACK_MESSAGE`, `WHATSAPP_ENABLED`, `META_APP_ID`, `META_APP_SECRET`, `META_VERIFY_TOKEN`, `META_PAGE_ID`, `META_PAGE_ACCESS_TOKEN`, `META_WA_PHONE_NUMBER_ID`, `META_WA_ACCESS_TOKEN`, `WATI_*` (unused).

**Zoho:** `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REDIRECT_URI`, `ZOHO_REFRESH_TOKEN`, `ZOHO_WEBHOOK_SECRET`, `ZOHO_API_DOMAIN` (`https://www.zohoapis.in`), `ZOHO_ACCOUNTS_DOMAIN`.

**Frontend (Vercel):** `VITE_API_BASE_URL` = `https://leadcaller-backend-production.up.railway.app`.

---

## 11. Deployment process

**Backend (Railway, CLI):**
```bash
cd leadcaller/backend
railway up --service leadcaller-backend --ci      # builds Dockerfile, runs alembic upgrade head
# wait for status Online, then: curl .../health
```
- Railway CLI: `/opt/homebrew/bin/railway` (logged in as soilsystems.ai@gmail.com).
- The Dockerfile runs `alembic upgrade head && uvicorn app.main:app`.
- Env changes via `railway variables --service leadcaller-backend --set "KEY=VALUE"` (triggers a restart).
- ⚠️ Migrations run over the pooler — keep them lock-light (no `CREATE INDEX CONCURRENTLY`). If a migration hangs, check for idle-in-transaction zombies (`pg_stat_activity`).

**Frontend (Vercel, CLI):**
```bash
cd leadcaller/frontend
npm run build
vercel --prod --yes
vercel alias set <new-deployment-url> leadcaller-dashboard.vercel.app
```

**Tests:** `cd leadcaller/backend && source .venv/bin/activate && python -m pytest -q` (90 passing).

**Local dev:** `./start-dev.sh` (backend + ngrok). Migrations target the **same prod Supabase DB**, so be careful running them locally.

**Third-party webhook URLs (all → Railway):**
- Retell agent webhook: `…/webhooks/retell/call-completed`
- Retell phone-number inbound webhook: `…/webhooks/retell/inbound`
- Exotel status callback: `…/webhooks/exotel/status` (also `EXOTEL_STATUS_CALLBACK`)
- Exotel WhatsApp DLR: `…/webhooks/whatsapp/status`
- Zoho/Meta: pull/native integration — no push webhook to update.

---

## 12. Next immediate tasks

1. **Confirm the outbound speak-first test** (prewarm + `start_speaker=user`, currently deployed & enabled). If Retell honors the per-call override → keep it. If not → either set "Who speaks first → User" in the Retell **dashboard** + republish, or set `PREWARM_RETELL_LEG=False` to revert to clean-greeting-with-ringback.
2. **Get the Exotel SIP trunk termination URI** (reply to Exotel support thread, ref trunk `trmum1422b7d4503d1d2c7571a5i`). This unblocks **Retell direct origination** — the only way to get one ring + instant greeting + no silence.
3. **Meta Business Verification** (business.facebook.com → Security Center) to lift WhatsApp marketing caps.
4. **Decide visit feedback delivery:** keep plain text (24h-window only) or create a `visit_feedback` **template** for anytime delivery.
5. **(Optional) stale `in_progress` call-job sweeper** + clean up historical duplicate `exotel:` attempt rows.

---

## 13. Quick reference — key IDs

| Thing | Value |
|---|---|
| ExoPhone / business number | `+918046376848` |
| Retell agent | `agent_f97c51d6f6a367f30e57df6f99` (published v13) |
| Retell LLM | `llm_74885ab2c053c5f36aadc631f828` |
| Exotel account | `kumarenterprise1` |
| Exotel SIP trunk | `trmum1422b7d4503d1d2c7571a5i` |
| Post-call template | `woods_and_spices` (Document header = brochure PDF) |
| Brochure PDF | `https://www.soilsystems.in/_files/ugd/6c151e_1f49d9ce4c1242cdbc5550f67ca0d18d.pdf` |
| Backend | `https://leadcaller-backend-production.up.railway.app` |
| Dashboard | `https://leadcaller-dashboard.vercel.app` |
| GitHub | `https://github.com/soilsystems/retell-calling-agent` |
