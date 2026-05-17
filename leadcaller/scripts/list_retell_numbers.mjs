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

const env = loadEnv(".env");
const apiKey = env.RETELL_API_KEY;
if (!apiKey || /replace|your-|placeholder/i.test(apiKey)) {
  throw new Error("RETELL_API_KEY is missing or still a placeholder");
}

const response = await fetch("https://api.retellai.com/v2/list-phone-numbers", {
  headers: { Authorization: `Bearer ${apiKey}` },
});
const data = await response.json().catch(() => ({}));
if (!response.ok) {
  throw new Error(`Retell list-phone-numbers failed (${response.status}): ${JSON.stringify(data)}`);
}

const items = data.items || [];
if (items.length === 0) {
  console.log("No Retell phone numbers found for this API key.");
  process.exit(0);
}

for (const item of items) {
  const outboundCountries = item.allowed_outbound_country_list?.join(",") || "all/unspecified";
  const outboundAgents = (item.outbound_agents || []).map((agent) => agent.agent_id).join(",") || "none";
  console.log(`${item.phone_number} | type=${item.phone_number_type} | outbound_countries=${outboundCountries} | outbound_agents=${outboundAgents}`);
}
