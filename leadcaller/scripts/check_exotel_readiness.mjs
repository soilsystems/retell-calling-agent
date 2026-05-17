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

function isReady(value) {
  return Boolean(value) && !/replace|your-|placeholder|xxxxxxxxxx/i.test(value);
}

const env = loadEnv(".env");
const requiredKeys = [
  "RETELL_API_KEY",
  "RETELL_AGENT_ID",
  "EXOTEL_PHONE_NUMBER",
  "EXOTEL_TERMINATION_URI",
  "EXOTEL_SIP_AUTH_USERNAME",
  "EXOTEL_SIP_AUTH_PASSWORD",
  "EXOTEL_TRANSPORT",
];

let missing = 0;
for (const key of requiredKeys) {
  const ready = isReady(env[key]);
  if (!ready) missing += 1;
  const value =
    ready && key.includes("PHONE")
      ? env[key]
      : ready
        ? `<set len=${env[key].length}>`
        : "<missing>";
  console.log(`${ready ? "OK" : "MISSING"} ${key}=${value}`);
}

const phone = env.EXOTEL_PHONE_NUMBER || "";
if (isReady(phone) && !/^\+[1-9]\d{7,14}$/.test(phone)) {
  console.log("MISSING EXOTEL_PHONE_NUMBER must be E.164, for example +91XXXXXXXXXX");
  missing += 1;
}

const transport = env.EXOTEL_TRANSPORT || "";
if (isReady(transport) && !["TLS", "TCP", "UDP"].includes(transport)) {
  console.log("MISSING EXOTEL_TRANSPORT must be TLS, TCP, or UDP");
  missing += 1;
}

if (missing > 0) {
  console.log(`Readiness: blocked, ${missing} item(s) need attention.`);
  process.exit(1);
}

console.log("Readiness: Exotel values are present for Retell import.");
