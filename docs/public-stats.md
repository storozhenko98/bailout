# Public usage totals

The homepage shows two aggregate counters. No analytics SDK, cookies, local
storage, installation identifiers, location analytics, or extra CLI telemetry
requests are used. This is separate from the existing short-lived daily IP hashes
used for [abuse prevention](service-limits.md).

## What counts

- **Downloads:** GitHub's `download_count` for the three supported binary archives
  across public, stable releases. Checksums, source archives, drafts and prereleases
  are excluded. Downloads include installs, automatic updates, repeats and testing;
  they are not unique people or verified completed installations. GitHub determines
  the underlying download count. The server retains each public asset's highest
  observed count, so deleting an old asset does not remove downloads already seen.
- **Requests processed:** one per hosted `/v1/chat` HTTP request whose backend
  returns a successful HTTP status. A streaming request counts when the backend
  accepts the stream; it may still fail or be cancelled later. Client retries count
  separately; provider fallbacks inside one request do not. Model-list reads,
  website visits, stats reads, gate refusals and backend HTTP errors do not count.
  Self-hosted requests are not reported. Recording is best effort: a storage failure
  must not interrupt a model response.

The request counter begins at the `requests.since` timestamp in the public
endpoint. Earlier requests were not recorded and cannot be reconstructed reliably.
We do not manufacture a historical total. Downloads include older release assets
still available from GitHub when counting began.

## Persistence and freshness

`GET https://api.bailout.dev/v1/stats` (also `https://bailout.dev/v1/stats`) returns
`downloads.total`, `downloads.updated_at`, `requests.total`, `requests.since` and
`requests.updated_at`, plus a documentation link. Unknown downloads are `null`,
not zero. Download refresh health is also public: `last_attempt_at`,
`next_refresh_at`, `last_error` (a fixed diagnostic code or `null`) and
`consecutive_failures`. No upstream error bodies or client information are exposed.
There is no public write route.

The existing globally named `global-v1` SQLite Durable Object stores the request
integer, its starting timestamp and download observations. Counters survive
deployments and quota-window expiry; they do not reset monthly. Do not delete or
rename this object. No prompts, commands, file contents, credentials, user IDs,
client IPs or per-request event records are added to the statistics table.

Visible pages refresh every 30 seconds and stop polling while hidden. Cloudflare
caches the response internally for 30 seconds using a fixed key; browser responses
use `no-store` to avoid a longer zone-level browser cache. Arbitrary query parameters
cannot bypass it. A Durable Object alarm checks GitHub every two minutes, even
without website visitors. There is only one shared refresh across the service;
page views cannot multiply source requests. Snapshots return immediately while
the source refreshes in the background. A successful check advances the timestamp
even if the count has not changed.

Configure a dedicated GitHub read-only token in the Cloudflare
`GITHUB_STATS_TOKEN` secret so refreshes do not share the anonymous egress IP's
60-request hourly quota. For a hosted deployment, use a fine-grained token
with public-repository read-only access (or Contents: read for only your release
repository), then run `npx wrangler secret put GITHUB_STATS_TOKEN --config
gateway.wrangler.jsonc` in `worker`. Never put the token in source, the website,
or a client binary. Redirects are refused so the header cannot leave the fixed
GitHub API destination. Anonymous operation remains supported for self-hosting,
but is susceptible to shared-IP rate limits. Renew an expiring token before its
expiry; authentication failures remain visible in `downloads.last_error`.

Transient errors retry after 30 seconds, then one, two, four, and five minutes.
Repeated failures remain capped at a five-minute interval; GitHub's `Retry-After`
and exhausted rate-limit reset times take precedence. Both the schedule and retry
state survive restarts. Each complete source refresh has an eight-second deadline.
The last good total and timestamp remain intact on failures, including partial
pagination failures. GitHub may update its own source counts later: this is polling,
not an instantaneous record of a completed installation. Under normal conditions,
downloads appear within about three minutes of a change in GitHub's API, and
request totals usually lag by up to one minute.

Green dots mean a recent counter snapshot (five minutes for downloads and three
minutes for requests), not WebSocket streaming or guaranteed model availability.
A failed page refresh or stale source dims the dot and keeps
the last known number. Unavailable counts display a dash. Hover for the exact
number and snapshot time; compact labels include `1k`, `11k`, `101k`, `1M`, `1.1M`.

## Cost and privacy boundaries

The counters reuse requests already required to run Bailout. The binary and
installer are unchanged. The statistics endpoint is cached and burst-limited,
remains readable during the model-service cutoff, and never calls a model.
It still uses a small amount of hosting capacity; it is not a hard invoice cap.

GitHub and Cloudflare still receive normal connection metadata to serve downloads
and HTTP requests. Conversations still pass through OpenRouter and the chosen
model provider under their own policies. Aggregate statistics do not change that
data flow or enable conversation logging. Worker observability remains disabled.

Source: [GitHub release API](https://docs.github.com/en/rest/releases/releases),
[Cloudflare Cache API](https://developers.cloudflare.com/workers/runtime-apis/cache/).
