# Exotel + Retell SIP Setup

This is the remaining live-calling work for LeadCaller. Supabase, Zoho access, Retell API access, ngrok, FastAPI, and the dashboard are already prepared. The open item is connecting a real caller number through Exotel so Retell can use it as `from_number`.

## What To Confirm In The Google Meet

Ask Exotel for these exact values:

- Exotel account SID
- API key and API token
- API subdomain, usually something like `api.in.exotel.com`
- Exophone/DID to use as caller ID, in E.164 format like `+91XXXXXXXXXX`
- SIP trunk SID
- SIP termination URI/domain that Retell should call for outbound SIP termination
- SIP digest username
- SIP digest password
- Transport: prefer `TLS`; fallback `TCP` only if needed
- Whether India mobile outbound calling is enabled for this number and trunk

## Exotel Side

According to Exotel SIP trunking docs, outbound SIP to PSTN needs:

- A SIP trunk
- A phone number mapped to the trunk
- SIP digest credentials or allowed IPs
- Destination URI configured on the trunk
- SIP signaling using TLS `443` or TCP `5070`
- RTP media path allowed on UDP `10000-40000`

For Retell custom telephony, SIP digest credentials are usually the right fit because Retell is the SIP client/platform and does not give you a fixed local server IP from this project.

## Retell Side

Retell must know about the Exotel number before `create-phone-call` will accept it as `from_number`.

Retell import needs:

- `phone_number`: the Exotel DID, e.g. `+91XXXXXXXXXX`
- `termination_uri`: Exotel SIP termination URI
- `sip_trunk_auth_username`: Exotel SIP digest username
- `sip_trunk_auth_password`: Exotel SIP digest password
- `transport`: `TLS` or `TCP`
- outbound agent binding to `RETELL_AGENT_ID`
- allowed outbound country list containing `IN`

After import, set:

```env
RETELL_FROM_NUMBER=<same as EXOTEL_PHONE_NUMBER>
```

## Local Commands

Check if all required Exotel/Retell env values are present:

```powershell
node scripts/check_exotel_readiness.mjs
```

Import the Exotel number into Retell:

```powershell
node scripts/import_exotel_number_to_retell.mjs
```

List Retell numbers after import:

```powershell
node scripts/list_retell_numbers.mjs
```

Place a controlled test call to Ravi after import:

```powershell
node scripts/check_zoho_retell_flow.mjs --name ravi --call
```

## Current Expected State

Before the Exotel import, Retell call creation fails with:

```text
Item <RETELL_FROM_NUMBER> not found from phone-number
```

After Exotel import succeeds, Retell should list the imported number as `custom`, and `create-phone-call` should accept that number as `from_number`.

## References

- Exotel Dynamic SIP Trunking: https://docs.exotel.com/dynamic-sip-trunking
- Exotel call directions and modes: https://docs.exotel.com/dynamic-sip-trunking/call-directions
- Exotel quick start: https://docs.exotel.com/dynamic-sip-trunking/exotel-sip-trunking-quick-start
- Exotel network/firewall requirements: https://docs.exotel.com/dynamic-sip-trunking/network-and-firewall-configuration
- Retell import phone number: https://docs.retellai.com/api-references/import-phone-number
