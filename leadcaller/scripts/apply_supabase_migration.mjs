import fs from "node:fs";
import path from "node:path";
import { Client } from "pg";

const root = process.cwd();
const envPath = path.join(root, ".env");
const sqlPath = path.join(root, "scripts", "supabase_initial.sql");

function loadEnv(filePath) {
  const env = {};
  for (const line of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index === -1) continue;
    env[trimmed.slice(0, index)] = trimmed.slice(index + 1);
  }
  return env;
}

const env = loadEnv(envPath);
const rawUrl = env.DATABASE_URL;
if (!rawUrl) {
  throw new Error("DATABASE_URL is missing from .env");
}

function toPgConnectionString(value) {
  const normalized = value.replace(/^postgresql\+asyncpg:\/\//, "postgresql://");
  const match = normalized.match(/^(postgresql:\/\/)([^:]+):(.+)@([^/]+)(\/.*)$/);
  if (!match) return normalized;

  const [, scheme, user, password, host, rest] = match;
  return `${scheme}${encodeURIComponent(user)}:${encodeURIComponent(decodeURIComponent(password))}@${host}${rest}`;
}

const connectionString = toPgConnectionString(rawUrl);
const client = new Client({
  connectionString,
  ssl: { rejectUnauthorized: false },
});

await client.connect();
try {
  await client.query("BEGIN");
  await client.query(fs.readFileSync(sqlPath, "utf8"));
  await client.query("COMMIT");
  const result = await client.query(`
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN (
        'leads',
        'call_jobs',
        'call_attempts',
        'webhook_events',
        'crm_sync_logs',
        'followups',
        'campaign_metrics',
        'zoho_tokens',
        'alembic_version'
      )
    ORDER BY table_name
  `);
  const rlsResult = await client.query(`
    SELECT relname
    FROM pg_class
    WHERE relnamespace = 'public'::regnamespace
      AND relname IN (
        'leads',
        'call_jobs',
        'call_attempts',
        'webhook_events',
        'crm_sync_logs',
        'followups',
        'campaign_metrics',
        'zoho_tokens'
      )
      AND relrowsecurity = true
    ORDER BY relname
  `);
  const indexResult = await client.query(`
    SELECT indexname
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND indexname IN (
        'ix_leads_phone',
        'ix_leads_zoho_lead_id',
        'ix_call_attempts_retell_call_id',
        'ix_webhook_events_idempotency_key'
      )
    ORDER BY indexname
  `);
  console.log(`Migration applied. Tables present: ${result.rows.map((row) => row.table_name).join(", ")}`);
  console.log(`RLS enabled: ${rlsResult.rows.map((row) => row.relname).join(", ")}`);
  console.log(`Indexes present: ${indexResult.rows.map((row) => row.indexname).join(", ")}`);
} catch (error) {
  await client.query("ROLLBACK");
  throw error;
} finally {
  await client.end();
}
