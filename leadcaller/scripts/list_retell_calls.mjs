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

console.log("Trying GET with Content-Type header...");
let response = await fetch("https://api.retellai.com/v2/list-calls", {
  method: "GET",
  headers: { 
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json"
  },
});

console.log(`GET response status: ${response.status}`);
let text = await response.text();
try {
  const json = JSON.parse(text);
  console.log("GET Result:", JSON.stringify(json, null, 2));
} catch (e) {
  console.log("GET Result Text:", text);
}

if (response.status === 405 || response.status === 415) {
  console.log("\nTrying POST...");
  response = await fetch("https://api.retellai.com/v2/list-calls", {
    method: "POST",
    headers: { 
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({})
  });
  console.log(`POST response status: ${response.status}`);
  text = await response.text();
  try {
    const json = JSON.parse(text);
    console.log("POST Result:", JSON.stringify(json, null, 2));
  } catch (e) {
    console.log("POST Result Text:", text);
  }
}
