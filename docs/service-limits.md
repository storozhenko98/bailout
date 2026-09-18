# Public service limits and errors

Bailout is temporary help for machine setup and recovery. The public API needs no
account, but its free model capacity and hosting allowance are shared.

## Fair use

The gateway uses Cloudflare's trusted connecting IP. All accepted API requests,
including model lists, status and API documentation, count toward these limits:

| Scope | Limit |
| --- | --- |
| Client IP | 60 requests per minute |
| Client IP | 600 requests per hour |
| Client IP | 2,000 requests per UTC day |
| All clients combined | 240 requests per minute |
| All clients combined, chat admissions | 120 requests per minute |

IPv6 addresses share a limit within their /64. People behind the same NAT share
an IP limit. An installation ID or user-supplied forwarding header cannot bypass
it. These are fixed windows, so adjacent windows can admit a boundary burst.
Cheap Cloudflare rate-limit bindings additionally reject bursts before the shared
gate (120/IP/minute and 480/location/minute); those preliminary limits are approximate.
The global SQLite Durable Object makes the final admission decision atomically.
These are gateway admissions, not promised model calls. Separately, the provider
ledger permits 18 OpenRouter inference attempts per rolling minute and 1,000 per
rolling 24 hours. Retries count. Upstream account limits or other applications on
the same account can reduce the available capacity. When explicitly enabled on a
verified Free organization, Groq defaults to 28 attempts per minute, 950 per
24 hours, 7,800 tokens per minute and 190,000 per 24 hours. Verified model-specific
quotas meter those models independently, as described below. Token quotas often
bind before request counts. `GET /v1/status` lists configured provider defaults;
it does not sum independent model pools or promise current upstream capacity.
A provider adapter being in the source does not mean it is live.
Each provider allows at most eight concurrent calls and each model at most two
(one for ZAI free routes).
A refusal never enables paid inference.

## Hosting allowance

The public deployment targets a $50 monthly hosting budget: $5 for Workers Paid,
$35 for conservatively reserved API work, and $10 headroom. This is **not a hard
Cloudflare invoice cap**. Taxes, other account services, traffic rejected before
the ledger, and continued requests after shutdown can still incur charges.

Before forwarding a request, the gate reserves $0.000300 for backend work, plus
$0.000010 for checking capacity. The Python Worker is limited to 5,000 CPU ms per
request; at $0.02/million CPU ms that costs at most approximately $0.000100,
before the extra allowance for gateway and storage work. The 50 ms gateway
Worker limits, body-size limit, and route allowlist bound individual requests.
No credit is taken for the plan's included usage. Failed or cancelled requests
are not refunded. The counter deliberately overestimates ordinary work; it is
not live billing data. Around 112,903 forwarded requests would consume the
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
| 503 | `service_unavailable` / `capacity_unavailable` | Capacity verification or backend connection failed; stop the current operation. |
| 429 | `upstream_rate_limited` / `provider_rate_limited` / `free_capacity_exhausted` | Bounded backend recovery could not obtain capacity; honor the returned delay. |
| 503 | `upstream_authentication` | Hosted service authentication failed; stop. |
| 429/503 | `upstream_quota` | Account allowance exhausted; stop. Never enable paid routing. |
| 503 | `upstream_policy` | Access or content policy blocked the request; stop without switching models. |
| 503/504 | `recovery_exhausted` | Four attempts or 120 seconds exhausted; display the error and wait. |
| 400 | `context_exhausted` | No qualified free route fits; history remains intact. Start a fresh task with `/new`. |
| 503 | `pricing_unavailable` / `no_free_models` | No safely eligible free route; stop. |
| 502 | `unexpected_cost` | Cost audit failed; stop and investigate the provider. |

ZAI uses HTTP 429 for several distinct conditions. Bailout classifies its
[business error codes](https://docs.z.ai/api-reference/api-code): `1302`/`1305`
receive bounded overload backoff; `1113`/`1308`/`1310` stop rapid retries for
balance or allowance restrictions; `1311` marks the model unavailable on the
account; `1313` stops for account policy. Errors may include the numeric
`provider_error_code` for diagnosis. Provider response bodies are never exposed
or logged, and no error enables paid access.
The shared ledger admits at most one in-flight request per ZAI model, matching
the lower concurrency available on its free Flash routes. Other eligible models
and independent providers remain available while that model is busy.

`GET /v1/status` reports the conservative reservation counter and limits, never
credentials or conversations. It also refuses when the allowance is exhausted.
`GET /health` checks the gateway, not model availability. The CLI displays policy
errors verbatim and does not automatically retry them. During streaming, only a
validated `done` event authorizes executing tools; partial responses do not.
After stream headers are sent, backend errors use an NDJSON `error` event under
HTTP 200 with the same machine-readable codes. Do not treat HTTP 200 alone as a
completed model response. Auto's internal provider retries share one admission
and a maximum of four attempts / 120 seconds. The CLI does not resubmit a failed
API request and multiply that budget. See [model recovery](model-recovery.md).

## Operator controls

`worker/gateway.wrangler.jsonc` contains `SERVICE_PAUSED` and the allowance. Set
`SERVICE_PAUSED` to `"true"` and deploy the gateway to refuse work immediately:

```sh
cd worker
npx wrangler deploy --config gateway.wrangler.jsonc
```

Restore it to `"false"` to resume; this does not reset the persistent budget.
A source change is intentionally required for a larger allowance. Do not expose
`api` directly. For this upgrade, deploy the gateway's new capacity endpoints with
`--var CHAT_ADMISSIONS_PER_MINUTE:18`, then deploy Python with its cross-Worker
`CAPACITY` binding, then redeploy the gateway without that temporary override.
This preserves the old admission limit until every upstream attempt is metered.
Deploy the static site after verifying the API. Never delete or rename the existing
`global-v1` Durable Object to reset quotas or budgets.

Cloudflare Billing budget alerts monitor actual account-wide metered spend;
they are separate from the conservative admission counter and can lag. Configure
email thresholds at $10, $25 and $40 of metered usage (fixed fees are separate).
Alerts do not stop usage. Do not interpret a lack of email as remaining capacity.
Review the live ledger and billing dashboard after changes to price or CPU limits.

The gate stores hourly aggregate reservations and daily IP hashes with short
expiry. Hashes are pseudonyms, not anonymization. Client rows expire after the
following UTC day and are cleaned when the gate next processes traffic. No
prompts, file contents or credentials are stored in the ledger. Provider attempt
timestamps, token reservations and leases have no client identity; these expire
after 24 hours and are cleaned on subsequent requests. Cooldowns also expire.

Separate [public usage totals](public-stats.md) retain a lifetime request integer
from the time counting is enabled, plus aggregate GitHub binary-download counts.
The cached, read-only `/v1/stats` endpoint does not consume the model allowance or
call Python/OpenRouter and stays available during a cutoff. It has a separate
burst-limit key; cache misses and counter writes still incur hosting work.

## Direct free-provider setup

Provider keys stay in the private Python Worker's encrypted secrets:
`OPENROUTER_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, `ZAI_API_KEY`, and
`VERCEL_AI_GATEWAY_API_KEY`. Missing keys disable their providers. Model discovery
alone never qualifies a model for serving users.

Groq and Mistral additionally require `FREE_ACCOUNTS`, a JSON configuration on the
Python Worker. Verify the account has no paid billing or automatic top-ups before
attesting it. The former `GROQ_FREE_ONLY` boolean is no longer sufficient. Example
shape (replace the timestamps with your actual verification and expiry):

```json
{
  "groq": {
    "tier": "free",
    "billing_disabled": true,
    "topups_disabled": true,
    "verified_at": "2026-09-17T00:00:00Z",
    "expires_at": "2026-09-24T00:00:00Z"
  }
}
```

Attestations expire after at most 31 days. Missing, expired, malformed or paid
account configuration disables the provider. This is operator verification, not
an assertion that the providers offer an API to detect account upgrades. Disable
the route before changing account billing. Use an isolated free account for Bailout.
Add a similarly verified `mistral` record to enable its free account tier.

When the account dashboard or live quota headers plus official documentation
confirm model-specific quotas, add `limits_by_model`
inside that account record, keyed by the provider's exact model ID. Each entry has
positive integer `rpm`, `rpd`, `tpm` and `tpd`, conservatively below the verified
limits. The shared Durable Object then meters each model independently; account
cooldowns and the eight-call provider concurrency limit still apply. Without that
verification, quotas stay in the conservative shared provider pool. Never add
keys/accounts to evade an upstream account's limits.

On 2026-09-18, Groq's live headers confirmed independent remaining-request counts
for `openai/gpt-oss-120b` and `qwen/qwen3.8-27b`, each with 1,000 RPD and 8,000 TPM.
The [official Free-plan table](https://console.groq.com/docs/rate-limits) also lists
30 RPM and 200,000 TPD for each. Bailout reserves 28 RPM, 950 RPD, 7,800 TPM and
190,000 TPD per model, retaining existing usage rather than resetting counters.

Vercel's [free-tier throttle is per model](https://vercel.com/docs/ai-gateway/rate-limits).
A live synthetic burst returned a temporary 429 on its sixth request, with an
upgrade suggestion mentioning credits; this was not exhausted credit. Bailout
paces each Vercel model to four requests per rolling minute, still under its
shared 10 RPM / 900 RPD gate, and respects upstream retry delays. A headerless
Vercel rate-limit response gets a 60-second model cooldown. A one-time migration
removes only the known erroneous account cooldown created at 17:01 UTC on
2026-09-18; it preserves usage and all subsequent cooldowns.

OpenRouter checks all live model and endpoint charges and enforces hard zero price
caps on every attempt. Z.AI checks its official pricing table before each attempt
and requires explicit Free input, cached input, cache storage, and output. Unknown
rows or changed documentation fail closed; its native model API has no equivalent
to OpenRouter's hard maximum-price field. Vercel checks its live catalog, rejects
unknown/nonzero fees and non-tool models, and sets `has: ["free"]` on every gateway
request. Vercel's free credits or a $5 spending limit do not qualify paid models.
An unexpected reported cost stops the request and cools down that provider for 24 hours.

Add configured provider names to the gateway's comma-separated `PROVIDER_POOL` so
status lists their admission ceilings. In addition to the OpenRouter and Groq
ceilings above, default operator ceilings are Mistral 1 RPM / 900 RPD / 48,000 TPM /
450,000 TPD, and Z.AI/Vercel each 10 RPM / 900 RPD. These are conservative application
limits, not claims about your provider entitlement. Verify actual account limits
before activation. Upstream refusals always win; no limit authorizes paid usage.

The [qualification and staged rollout guide](model-qualification.md) covers the
nightly workflow, two distinct operator secrets, initial evidence, and switching
the public backend only after models genuinely qualify. `/v1/status` also reports
ranking freshness. All metadata/routing controls remain private to the operator.
