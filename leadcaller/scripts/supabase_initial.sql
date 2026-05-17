CREATE EXTENSION IF NOT EXISTS "pgcrypto";

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'language_preference') THEN
        CREATE TYPE language_preference AS ENUM ('hindi', 'english', 'kannada');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'call_job_status') THEN
        CREATE TYPE call_job_status AS ENUM ('pending', 'in_progress', 'completed', 'failed', 'cancelled');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'call_attempt_status') THEN
        CREATE TYPE call_attempt_status AS ENUM ('initiated', 'ringing', 'answered', 'no_answer', 'busy', 'failed', 'completed');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'webhook_source') THEN
        CREATE TYPE webhook_source AS ENUM ('zoho', 'retell');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'followup_status') THEN
        CREATE TYPE followup_status AS ENUM ('pending', 'created', 'failed');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS leads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    zoho_lead_id text UNIQUE NOT NULL,
    name varchar(100) NOT NULL,
    phone varchar(16) NOT NULL,
    email varchar(320),
    city varchar(120),
    language_preference language_preference NOT NULL DEFAULT 'english',
    source varchar(120),
    campaign varchar(120),
    created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
    updated_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE INDEX IF NOT EXISTS ix_leads_phone ON leads (phone);
CREATE INDEX IF NOT EXISTS ix_leads_zoho_lead_id ON leads (zoho_lead_id);

CREATE TABLE IF NOT EXISTS call_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    status call_job_status NOT NULL DEFAULT 'pending',
    scheduled_at timestamptz NOT NULL,
    started_at timestamptz,
    completed_at timestamptz,
    retry_count integer NOT NULL DEFAULT 0,
    max_retries integer NOT NULL DEFAULT 3,
    created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
    updated_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS call_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_job_id uuid NOT NULL REFERENCES call_jobs(id) ON DELETE CASCADE,
    retell_call_id text UNIQUE NOT NULL,
    attempt_number integer NOT NULL,
    status call_attempt_status NOT NULL DEFAULT 'initiated',
    recording_url text,
    transcript text,
    summary text,
    structured_data jsonb,
    started_at timestamptz,
    ended_at timestamptz,
    duration_seconds integer
);

CREATE INDEX IF NOT EXISTS ix_call_attempts_retell_call_id ON call_attempts (retell_call_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source webhook_source NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    processed boolean NOT NULL DEFAULT false,
    idempotency_key text UNIQUE NOT NULL,
    received_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE INDEX IF NOT EXISTS ix_webhook_events_idempotency_key ON webhook_events (idempotency_key);

CREATE TABLE IF NOT EXISTS crm_sync_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id uuid REFERENCES leads(id) ON DELETE SET NULL,
    operation text NOT NULL,
    success boolean NOT NULL,
    error_message text,
    synced_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS followups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    call_attempt_id uuid REFERENCES call_attempts(id) ON DELETE SET NULL,
    scheduled_at timestamptz NOT NULL,
    zoho_task_id text,
    status followup_status NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS campaign_metrics (
    campaign text NOT NULL,
    date date NOT NULL,
    total_leads integer NOT NULL,
    calls_made integer NOT NULL,
    answered integer NOT NULL,
    hot_leads integer NOT NULL,
    conversion_rate double precision NOT NULL,
    PRIMARY KEY (campaign, date)
);

CREATE TABLE IF NOT EXISTS zoho_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    access_token text NOT NULL,
    refresh_token text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
    updated_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_sync_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE followups ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE zoho_tokens ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'leads',
        'call_jobs',
        'call_attempts',
        'webhook_events',
        'crm_sync_logs',
        'followups',
        'campaign_metrics',
        'zoho_tokens'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_policies
            WHERE schemaname = 'public'
              AND tablename = table_name
              AND policyname = 'service_role_all_' || table_name
        ) THEN
            EXECUTE format(
                'CREATE POLICY %I ON %I FOR ALL USING (true) WITH CHECK (true)',
                'service_role_all_' || table_name,
                table_name
            );
        END IF;
    END LOOP;
END $$;

CREATE MATERIALIZED VIEW IF NOT EXISTS campaign_metrics_nightly AS
SELECT
    l.campaign,
    date_trunc('day', l.created_at)::date AS date,
    count(DISTINCT l.id)::integer AS total_leads,
    count(DISTINCT ca.id)::integer AS calls_made,
    count(DISTINCT ca.id) FILTER (WHERE ca.status IN ('answered', 'completed'))::integer AS answered,
    count(DISTINCT ca.id) FILTER (WHERE ca.structured_data->>'interest_level' = 'Hot')::integer AS hot_leads,
    COALESCE(
        (
            count(DISTINCT ca.id) FILTER (WHERE ca.structured_data->>'interest_level' = 'Hot')::double precision
            / NULLIF(count(DISTINCT l.id), 0)
        ),
        0
    ) AS conversion_rate
FROM leads l
LEFT JOIN call_jobs cj ON cj.lead_id = l.id
LEFT JOIN call_attempts ca ON ca.call_job_id = cj.id
GROUP BY l.campaign, date_trunc('day', l.created_at)::date;

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num varchar(32) NOT NULL PRIMARY KEY
);

INSERT INTO alembic_version (version_num)
VALUES ('001_initial')
ON CONFLICT (version_num) DO NOTHING;
