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
  if (!value || /replace|your-|placeholder|xxxxxxxxxx/i.test(value)) {
    throw new Error(`${key} is missing or still a placeholder`);
  }
  return value;
}

const env = loadEnv(".env");
const endpoint = env.RETELL_IMPORT_PHONE_NUMBER_ENDPOINT || "https://api.retellai.com/v2/import-phone-number";
const phoneNumber = required(env, "EXOTEL_PHONE_NUMBER");
const agentId = required(env, "RETELL_AGENT_ID");

const body = {
  phone_number: phoneNumber,
  termination_uri: required(env, "EXOTEL_TERMINATION_URI"),
  sip_trunk_auth_username: required(env, "EXOTEL_SIP_AUTH_USERNAME"),
  sip_trunk_auth_password: required(env, "EXOTEL_SIP_AUTH_PASSWORD"),
  transport: env.EXOTEL_TRANSPORT || "TLS",
  outbound_agents: [{ agent_id: agentId, weight: 1 }],
  inbound_agents: [{ agent_id: agentId, weight: 1 }],
  nickname: "LeadCaller Exotel",
  allowed_outbound_country_list: ["IN"],
  allowed_inbound_country_list: ["IN"],
};

const response = await fetch(endpoint, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${required(env, "RETELL_API_KEY")}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify(body),
});

const data = await response.json().catch(() => ({}));
if (!response.ok) {
  throw new Error(`Retell Exotel import failed (${response.status}): ${JSON.stringify(data)}`);
}

console.log(`Imported Exotel number into Retell: ${data.phone_number || phoneNumber}`);
console.log(`Phone number type: ${data.phone_number_type || "unknown"}`);
console.log(`Set RETELL_FROM_NUMBER=${phoneNumber}`);
