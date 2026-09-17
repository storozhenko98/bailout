# Public service limits and errors

Bailout is temporary help for machine setup and recovery. The public API needs no
account, but its free model capacity and hosting allowance are shared.

## Fair use

The gateway uses Cloudflare's trusted connecting IP. All accepted API requests,
including model lists, status and API documentation, count toward these limits:

| Scope | Limit |
| --- | --- |
| Client IP | 30 requests per minute |
| Client IP | 300 requests per hour |
| Client IP | 1,000 requests per UTC day |
| All clients combined | 120 requests per minute |
| All clients combined, chat | 18 requests per minute |

IPv6 addresses share a limit within their /64. People behind the same NAT share
an IP limit. An installation ID or user-supplied forwarding header cannot bypass
it. These are fixed windows, so adjacent windows can admit a boundary burst.
Cheap Cloudflare rate-limit bindings additionally reject bursts before the shared
gate (60/IP/minute and 240/location/minute); those preliminary limits are approximate.
The global SQLite Durable Object makes the final admission decision atomically.
OpenRouter may impose lower quotas. A refusal never selects a paid model.

## Hosting allowance

The public deployment targets a $50 monthly hosting budget: $5 for Workers Paid,
$35 for conservatively reserved API work, and $10 headroom. This is **not a hard
Cloudflare invoice cap**. Taxes, other account services, traffic rejected before
the ledger, and continued requests after shutdown can still incur charges.

Before forwarding a request, the gate reserves $0.000110 for backend work, plus
$0.000010 for checking capacity. The Python Worker is limited to 5,000 CPU ms per
request; at $0.02/million CPU ms that costs at most approximately $0.000100,
before the extra allowance for gateway and storage work. The 50 ms JavaScript
Worker limits, body-size limit, and route allowlist bound individual requests.
No credit is taken for the plan's included usage. Failed or cancelled requests
are not refunded. The counter deliberately overestimates ordinary work; it is
not live billing data. Around 291,666 forwarded requests would consume the
allowance, fewer when other gate requests are included.

Reservations persist in one named Durable Object across deployments. Hourly
buckets cover at least the preceding 31 days (up to 31 days plus one hour), avoiding
a double allowance across calendar/billing boundaries. Capacity returns as older
reservations expire. Lowering the configured allowance applies immediately;
raising it above $35 is rejected. Do not delete the ledger or change its object
name to reset usage. Python has no public domain, workers.dev or preview URL;
the homepage's legacy API paths also pass through the guard.

At exhaustion, the gateway stops forwarding new work to FastAPI/OpenRouter and
returns HTTP 503 with `code: "budget_exhausted"`. Already admitted requests can
finish; their full processing allowance was reserved in advance. If the ledger
cannot be checked, service fails closed. A cached exhaustion refusal lasts at
most 60 seconds. The homepage and installer remain available.

## Consumer contract

Errors use an `error` string for compatibility with existing Bailout versions:

```json
{
  "error": "Bailout's shared hosting allowance is exhausted. New model requests are paused to control costs. Capacity begins returning at 2026-10-18T21:00:00.000Z. You have not been charged. See https://bailout.dev/docs/#service-limits",
  "code": "budget_exhausted",
  "retry_after_seconds": 3600,
  "resets_at": "2026-10-18T21:00:00.000Z",
  "docs": "https://bailout.dev/docs/#service-limits"
}
```

Dates and delays above are illustrative. Actual responses calculate both from the
ledger and include the same delay in the HTTP `Retry-After` header. The timestamp
is the earliest capacity return, not a promise that an entire session will fit.

| HTTP | Code | Consumer behavior |
| --- | --- | --- |
| 503 | `budget_exhausted` | Show the message and stop automatic retries. Wait until the retry time or use a separately hosted backend. |
| 429 | `client_rate_limited` | Wait for `Retry-After`; do not rotate identities to evade the quota. |
| 429 | `capacity_busy` | Shared capacity is busy; honor `Retry-After` and add jitter when retrying. |
| 503 | `service_paused` | Operator pause; display the message and wait. |
| 503 | `service_unavailable` | Capacity verification or backend connection failed; stop the current operation. |
| 429 | `upstream_rate_limited` | OpenRouter's free capacity is unavailable; wait. This legacy backend error may omit a retry timestamp. |

`GET /v1/status` reports the conservative reservation counter and limits, never
credentials or conversations. It also refuses when the allowance is exhausted.
`GET /health` checks the gateway, not model availability. The CLI displays policy
errors verbatim and does not automatically retry them. During streaming, only a
validated `done` event authorizes executing tools; partial responses do not.

## Operator controls

`worker/gateway.wrangler.jsonc` contains `SERVICE_PAUSED` and the allowance. Set
`SERVICE_PAUSED` to `"true"` and deploy the gateway to refuse work immediately:

```sh
cd worker
npx wrangler deploy --config gateway.wrangler.jsonc
```

Restore it to `"false"` to resume; this does not reset the persistent budget.
A source change is intentionally required for a larger allowance. Do not expose
`api` directly. Deploy Python first, then the gateway, then the static-site Worker.

Cloudflare Billing budget alerts monitor actual account-wide metered spend;
they are separate from the conservative admission counter and can lag. Configure
email thresholds at $10, $25 and $40 of metered usage (fixed fees are separate).
Alerts do not stop usage. Do not interpret a lack of email as remaining capacity.
Review the live ledger and billing dashboard after changes to price or CPU limits.

The gate stores hourly aggregate reservations and daily IP hashes with short
expiry. Hashes are pseudonyms, not anonymization. Client rows expire after the
following UTC day and are cleaned when the gate next processes traffic. No
prompts, file contents or credentials are stored in the ledger.

Separate [public usage totals](public-stats.md) retain a lifetime request integer
from the time counting is enabled, plus aggregate GitHub binary-download counts.
The cached, read-only `/v1/stats` endpoint does not consume the model allowance or
call Python/OpenRouter and stays available during a cutoff. It has a separate
burst-limit key; cache misses and counter writes still incur hosting work.
