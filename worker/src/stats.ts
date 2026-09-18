import type { Env, Storage } from './types.js';
interface StatsRow { [key: string]: SqlStorageValue; requests: number; since: string; downloads: string; downloads_updated: string | null; next_refresh: number }
interface RefreshRow { [key: string]: SqlStorageValue; last_attempt: string | null; last_error: string | null; failures: number }
type FetcherFunction = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
// Aggregate counters only. No request bodies, IPs, installation IDs or event log.
export const DOWNLOAD_REFRESH_MS = 2 * 60_000;
export const DOWNLOAD_RETRY_MS = 30_000;
const ASSET = /^bailout-(?:macos-arm64|linux-x64|linux-arm64)\.tar\.gz$/;
const RELEASES = 'https://api.github.com/repos/storozhenko98/bailout/releases';

class DownloadError extends Error {
  code: string; retryAt: number;
  constructor(code: string, retryAt = 0) { super(code); this.code = code; this.retryAt = retryAt; }
}

function rateLimitReset(headers: Headers, now: number) {
  const retry = headers.get('Retry-After');
  const retryAt = retry && /^\d+$/.test(retry) ? now + Number(retry) * 1000 : Date.parse(retry || '');
  const reset = headers.get('X-RateLimit-Remaining') === '0' ? Number(headers.get('X-RateLimit-Reset')) * 1000 : 0;
  return Math.max(now + 60_000, Number.isFinite(retryAt) ? retryAt : 0, Number.isFinite(reset) ? reset + 1000 : 0);
}

export async function releaseDownloads(fetcher: FetcherFunction = fetch, now = Date.now()) {
  const counts: Record<string, number> = {};
  // One deadline bounds the whole refresh, including bodies and pagination.
  const signal = AbortSignal.timeout(8000);
  for (let page = 1; page <= 10; page++) {
    let result;
    try {
      result = await fetcher(`${RELEASES}?per_page=100&page=${page}`, {
        headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'bailout-public-stats', 'X-GitHub-Api-Version': '2022-11-28' },
        // Workers supports manual/follow only. A redirect is rejected below
        // as a non-2xx response, without forwarding the credential elsewhere.
        cache: 'no-store', redirect: 'manual', signal,
      });
    } catch {
      throw new DownloadError(signal.aborted ? 'github_timeout' : 'github_network_error');
    }
    if (!result.ok) {
      const retryAt = [403, 429].includes(result.status) ? rateLimitReset(result.headers, now) : 0;
      await result.body?.cancel();
      throw new DownloadError(`github_http_${result.status}`, retryAt);
    }
    let releases;
    try { releases = await result.json(); }
    catch { throw new DownloadError(signal.aborted ? 'github_timeout' : 'github_invalid_response'); }
    if (!Array.isArray(releases)) throw new DownloadError('github_invalid_response');
    for (const release of releases) {
      if (!release || typeof release !== 'object') throw new DownloadError('github_invalid_response');
      if (release.draft || release.prerelease) continue;
      if (!Array.isArray(release.assets)) throw new DownloadError('github_invalid_response');
      for (const asset of release.assets) {
        if (!asset || typeof asset !== 'object') throw new DownloadError('github_invalid_response');
        if (!ASSET.test(asset.name)) continue;
        if (!Number.isSafeInteger(asset.id) || asset.id < 1 || !Number.isSafeInteger(asset.download_count) || asset.download_count < 0) throw new DownloadError('github_invalid_count');
        counts[asset.id] = asset.download_count;
      }
    }
    if (releases.length < 100) return counts;
  }
  throw new DownloadError('github_pagination_limit'); // Never publish a partial total.
}

export class PublicStats {
  storage: Storage; sql: SqlStorage; fetcher: FetcherFunction; authenticated: number; refreshing: Promise<void> | null;
  constructor(storage: Storage, now = Date.now(), fetcher: FetcherFunction = fetch, githubToken = '') {
    this.storage = storage;
    this.sql = storage.sql;
    // Only the fixed GitHub release endpoint sees this dedicated read-only key.
    // It is never persisted in statistics or returned to browsers/clients.
    this.fetcher = githubToken ? (url, options) => fetcher(url, {
      ...options, headers: { ...options?.headers, Authorization: `Bearer ${githubToken}` },
    }) : fetcher;
    this.authenticated = githubToken ? 1 : 0;
    this.refreshing = null;
    this.sql.exec('CREATE TABLE IF NOT EXISTS public_stats (id INTEGER PRIMARY KEY, requests INTEGER NOT NULL, since TEXT NOT NULL, downloads TEXT NOT NULL, downloads_updated TEXT, next_refresh INTEGER NOT NULL)');
    this.sql.exec('INSERT OR IGNORE INTO public_stats VALUES (1, 0, ?, ?, NULL, 0)', new Date(now).toISOString(), '{}');
    this.sql.exec('CREATE TABLE IF NOT EXISTS download_refresh (id INTEGER PRIMARY KEY, last_attempt TEXT, last_error TEXT, failures INTEGER NOT NULL)');
    this.sql.exec('INSERT OR IGNORE INTO download_refresh VALUES (1, NULL, NULL, 0)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS download_source (id INTEGER PRIMARY KEY, authenticated INTEGER NOT NULL)');
    this.sql.exec('INSERT OR IGNORE INTO download_source VALUES (1, 0)');
  }
  recordRequest() {
    this.sql.exec('UPDATE public_stats SET requests = requests + 1 WHERE id = 1');
  }
  async refresh(now = Date.now()) {
    if (this.refreshing) return this.refreshing;
    const row = this.sql.exec<StatsRow>('SELECT * FROM public_stats WHERE id = 1').one();
    // Upgrade the old 15-minute schedule once, without bypassing a provider's
    // Retry-After after this deployment has made its first attempt.
    const state = this.sql.exec<RefreshRow>('SELECT * FROM download_refresh WHERE id = 1').one();
    const source = this.sql.exec<{ authenticated: number }>('SELECT authenticated FROM download_source WHERE id = 1').one();
    if (row.next_refresh > now && state.last_attempt && source.authenticated === this.authenticated) {
      await this.schedule(row.next_refresh);
      return;
    }
    this.sql.exec('UPDATE public_stats SET next_refresh = ? WHERE id = 1', now + DOWNLOAD_REFRESH_MS);
    // Provisioning authentication changes from an IP quota to the account quota;
    // its first check need not wait for the old anonymous IP's reset deadline.
    this.sql.exec('UPDATE download_source SET authenticated = ? WHERE id = 1', this.authenticated);
    this.sql.exec('UPDATE download_refresh SET last_attempt = ? WHERE id = 1', new Date(now).toISOString());
    this.refreshing = this.refreshDownloads(now).finally(() => { this.refreshing = null; });
    return this.refreshing;
  }
  async schedule(when: number) {
    if (await this.storage.getAlarm() !== when) await this.storage.setAlarm(when);
  }
  snapshot(now = Date.now()) {
    const current = this.sql.exec<StatsRow>('SELECT * FROM public_stats WHERE id = 1').one();
    const state = this.sql.exec<RefreshRow>('SELECT * FROM download_refresh WHERE id = 1').one();
    return {
      downloads: { total: current.downloads_updated ? Object.values(JSON.parse(current.downloads) as Record<string, number>).reduce((a, b) => a + b, 0) : null,
        updated_at: current.downloads_updated, source: 'github_release_assets',
        last_attempt_at: state.last_attempt, next_refresh_at: new Date(current.next_refresh).toISOString(),
        last_error: state.last_error, consecutive_failures: state.failures },
      requests: { total: current.requests, since: current.since, updated_at: new Date(now).toISOString() },
      docs: 'https://bailout.dev/docs/#public-stats',
    };
  }
  async refreshDownloads(now: number) {
    // Arm the next run before network I/O so a terminated request cannot strand
    // the counter. Alarms continue even when nobody has the website open.
    await this.schedule(now + DOWNLOAD_REFRESH_MS);
    try {
      const observed = await releaseDownloads(this.fetcher, now);
      this.storage.transactionSync(() => {
        const previous: Record<string, number> = JSON.parse(this.sql.exec<{ downloads: string }>('SELECT downloads FROM public_stats WHERE id = 1').one().downloads);
        // Retain each asset's highest observed count, even if an old release is
        // deleted. These are public asset IDs, not identifiers for people.
        for (const [id, count] of Object.entries(observed)) previous[id] = Math.max(previous[id] ?? 0, count);
        const total = Object.values(previous).reduce((a, b) => a + b, 0);
        if (!Number.isSafeInteger(total)) throw new Error('Invalid total');
        this.sql.exec('UPDATE public_stats SET downloads = ?, downloads_updated = ? WHERE id = 1', JSON.stringify(previous), new Date(now).toISOString());
        this.sql.exec('UPDATE download_refresh SET last_error = NULL, failures = 0 WHERE id = 1');
      });
    } catch (error) {
      const failures = this.sql.exec<{ failures: number }>('SELECT failures FROM download_refresh WHERE id = 1').one().failures + 1;
      const delay = Math.min(5 * 60_000, DOWNLOAD_RETRY_MS * 2 ** Math.min(failures - 1, 4));
      const next = Math.max(now + delay, error instanceof DownloadError ? error.retryAt : 0);
      // Never store upstream bodies, raw exceptions, credentials or client data.
      this.sql.exec('UPDATE download_refresh SET last_error = ?, failures = ? WHERE id = 1', error instanceof DownloadError ? error.code : 'download_storage_error', failures);
      this.sql.exec('UPDATE public_stats SET next_refresh = ? WHERE id = 1', next);
      await this.schedule(next);
    }
  }
}

// The canonical cache key ignores query strings and user headers. Counters are
// public and read-only, including while the model service is paused/exhausted.
export async function publicStatsResponse(env: Env, cache = caches.default) {
  const key = new Request('https://api.bailout.dev/v1/stats');
  const cached = await cache.match(key);
  if (cached) return browserResponse(cached);
  const burst = await env.EDGE_GLOBAL_LIMIT.limit({ key: 'public-stats' });
  if (!burst.success) return Response.json({ error: 'Statistics are temporarily busy.' }, { status: 429, headers: { 'Retry-After': '60', 'Access-Control-Allow-Origin': 'https://bailout.dev' } });
  try {
    const stub = env.BUDGET.get(env.BUDGET.idFromName('global-v1'));
    const result = await stub.fetch('https://budget/stats');
    if (!result.ok) throw new Error('Statistics unavailable');
    const response = new Response(result.body, { headers: { 'Content-Type': 'application/json',
      'Cache-Control': 'public, max-age=30', 'Access-Control-Allow-Origin': 'https://bailout.dev', 'X-Content-Type-Options': 'nosniff' } });
    await cache.put(key, response.clone());
    return browserResponse(response);
  } catch {
    return Response.json({ error: 'Statistics are temporarily unavailable.' }, { status: 503,
      headers: { 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': 'https://bailout.dev' } });
  }
}

function browserResponse(cached: Response) {
  const result = new Response(cached.body, cached);
  // Keep the edge's explicit 30-second Cache API entry, but prevent the zone's
  // longer Browser Cache TTL from freezing counters in a visitor's tab.
  result.headers.set('Cache-Control', 'no-store');
  return result;
}
