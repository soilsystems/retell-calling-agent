import fs from "node:fs";

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

function required(env, key) {
  const value = env[key];
  if (!value || /replace|your-|placeholder/i.test(value)) {
    throw new Error(`${key} is missing or still a placeholder`);
  }
  return value;
}

function normalizeIndianPhone(value) {
  if (!value) return null;
  const digits = String(value).replace(/\D/g, "");
  if (/^91[6-9]\d{9}$/.test(digits)) return `+${digits}`;
  if (/^[6-9]\d{9}$/.test(digits)) return `+91${digits}`;
  return String(value).trim();
}

async function refreshZohoToken(env) {
  const url = new URL(`${env.ZOHO_ACCOUNTS_DOMAIN || "https://accounts.zoho.com"}/oauth/v2/token`);
  url.searchParams.set("refresh_token", required(env, "ZOHO_REFRESH_TOKEN"));
  url.searchParams.set("client_id", required(env, "ZOHO_CLIENT_ID"));
  url.searchParams.set("client_secret", required(env, "ZOHO_CLIENT_SECRET"));
  url.searchParams.set("redirect_uri", required(env, "ZOHO_REDIRECT_URI"));
  url.searchParams.set("grant_type", "refresh_token");

  const response = await fetch(url, { method: "POST" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`Zoho token refresh failed (${response.status}): ${JSON.stringify(data)}`);
  }
  return data.access_token;
}

async function fetchRecentZohoLeads(env, accessToken) {
  const apiDomain = env.ZOHO_API_DOMAIN || "https://www.zohoapis.com";
  const url = new URL(`${apiDomain}/crm/v6/Leads`);
  url.searchParams.set("fields", "id,Full_Name,First_Name,Last_Name,Phone,Mobile,Email,City,Lead_Source,Campaign");
  url.searchParams.set("per_page", "100");
  url.searchParams.set("sort_by", "Created_Time");
  url.searchParams.set("sort_order", "desc");

  const response = await fetch(url, {
    headers: { Authorization: `Zoho-oauthtoken ${accessToken}` },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`Zoho lead fetch failed (${response.status}): ${JSON.stringify(data)}`);
  }
  const leads = data.data || [];
  if (leads.length === 0) {
    throw new Error("Zoho returned no leads");
  }
  return leads;
}

async function createZohoTestLead(env, accessToken) {
  const apiDomain = env.ZOHO_API_DOMAIN || "https://www.zohoapis.com";
  const phone = process.env.TEST_LEAD_PHONE || env.TEST_LEAD_PHONE || "+919876543210";
  const response = await fetch(`${apiDomain}/crm/v6/Leads`, {
    method: "POST",
    headers: {
      Authorization: `Zoho-oauthtoken ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      data: [
        {
          First_Name: "AI",
          Last_Name: "LeadCaller Test",
          Mobile: phone,
          Phone: phone,
          City: "Bengaluru",
          Lead_Source: "LeadCaller Integration Test",
          Description: "Temporary test lead created to verify Zoho to Retell phone-number flow.",
        },
      ],
    }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`Zoho test lead creation failed (${response.status}): ${JSON.stringify(data)}`);
  }
  const details = data.data?.[0]?.details || {};
  return {
    id: details.id,
    Full_Name: "AI LeadCaller Test",
    Mobile: phone,
    Phone: phone,
    City: "Bengaluru",
    Lead_Source: "LeadCaller Integration Test",
  };
}

function buildRetellPayload(env, lead) {
  const name = lead.Full_Name || [lead.First_Name, lead.Last_Name].filter(Boolean).join(" ") || "Lead";
  const phone = normalizeIndianPhone(lead.Mobile || lead.Phone);
  const campaign = lead.Campaign?.name || lead.Campaign || "";
  return {
    from_number: required(env, "RETELL_FROM_NUMBER"),
    to_number: phone,
    agent_id: required(env, "RETELL_AGENT_ID"),
    retell_llm_dynamic_variables: {
      lead_name: name,
      language: "english",
      city: lead.City || "",
      campaign,
      zoho_lead_id: lead.id,
    },
    webhook_url: `${required(env, "BASE_URL").replace(/\/$/, "")}/webhooks/retell/call-completed`,
  };
}

const env = loadEnv(".env");
const shouldCall = process.argv.includes("--call");
const shouldCreateTestLead = process.argv.includes("--create-test-lead");
const nameIndex = process.argv.indexOf("--name");
const targetName = nameIndex === -1 ? null : process.argv[nameIndex + 1]?.toLowerCase();

const accessToken = await refreshZohoToken(env);
const leads = shouldCreateTestLead ? [] : await fetchRecentZohoLeads(env, accessToken);
const lead = shouldCreateTestLead
  ? await createZohoTestLead(env, accessToken)
  : leads.find((candidate) => {
      const name = candidate.Full_Name || [candidate.First_Name, candidate.Last_Name].filter(Boolean).join(" ");
      const nameMatches = targetName ? name.toLowerCase().includes(targetName) : true;
      const phoneMatches = /^\+91[6-9]\d{9}$/.test(normalizeIndianPhone(candidate.Mobile || candidate.Phone) || "");
      return nameMatches && phoneMatches;
    });
if (!lead) {
  console.log(`Scanned ${leads.length} recent Zoho leads.`);
  for (const candidate of leads.slice(0, 5)) {
    const name = candidate.Full_Name || [candidate.First_Name, candidate.Last_Name].filter(Boolean).join(" ");
    console.log(`Lead skipped: id=${candidate.id}, name=${name || "<missing>"}, phone=${normalizeIndianPhone(candidate.Mobile || candidate.Phone) || "<missing>"}`);
  }
  throw new Error(
    targetName
      ? `No recent Zoho lead matching "${targetName}" has a valid Indian mobile number matching +91[6-9]XXXXXXXXX`
      : "No recent Zoho lead has a valid Indian mobile number matching +91[6-9]XXXXXXXXX"
  );
}
const retellPayload = buildRetellPayload(env, lead);

console.log(`Zoho leads scanned: ${leads.length}`);
console.log(`Zoho lead selected: id=${lead.id}, name=${retellPayload.retell_llm_dynamic_variables.lead_name}`);
console.log(`Phone selected for Retell to_number: ${retellPayload.to_number || "<missing>"}`);
console.log(`Retell webhook_url: ${retellPayload.webhook_url}`);

if (!retellPayload.to_number || !/^\+91[6-9]\d{9}$/.test(retellPayload.to_number)) {
  throw new Error("Fetched Zoho lead does not have a valid Indian mobile number for Retell");
}

if (!shouldCall) {
  console.log("Dry run only. Retell call was not created.");
  process.exit(0);
}

const response = await fetch("https://api.retellai.com/v2/create-phone-call", {
  method: "POST",
  headers: {
    Authorization: `Bearer ${required(env, "RETELL_API_KEY")}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify(retellPayload),
});
const data = await response.json().catch(() => ({}));
if (!response.ok) {
  throw new Error(`Retell call creation failed (${response.status}): ${JSON.stringify(data)}`);
}
console.log(`Retell call created: ${data.call_id || data.retell_call_id || JSON.stringify(data)}`);
