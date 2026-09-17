// Aggregate counters only. No request bodies, IPs, installation IDs or event log.
export const DOWNLOAD_REFRESH_MS = 15 * 60_000;
const ASSET = /^bailout-(?:macos-arm64|linux-x64|linux-arm64)\.tar\.gz$/;
const RELEASES = 'https://api.github.com/repos/storozhenko98/bailout/releases';

export async function releaseDownloads(fetcher = fetch) {
  const counts = {};
  for (let page = 1; page <= 10; page++) {
    const result = await fetcher(`${RELEASES}?per_page=100&page=${page}`, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'bailout-public-stats', 'X-GitHub-Api-Version': '2022-11-28' },
      signal: AbortSignal.timeout(5000),
    });
    if (!result.ok) throw new Error('Download counts unavailable');
    const releases = await result.json();
    if (!Array.isArray(releases)) throw new Error('Invalid release list');
    for (const release of releases) {
      if (release.draft || release.prerelease) continue;
      if (!Array.isArray(release.assets)) throw new Error('Missing release assets');
      for (const asset of release.assets) {
        if (!ASSET.test(asset.name)) continue;
        if (!Number.isSafeInteger(asset.id) || asset.id < 1 || !Number.isSafeInteger(asset.download_count) || asset.download_count < 0) throw new Error('Invalid download count');
        counts[asset.id] = asset.download_count;
      }
    }
    if (releases.length < 100) return counts;
  }
  throw new Error('Release pagination limit reached'); // Never publish a partial total.
}

export class PublicStats {
  constructor(storage, now = Date.now(), fetcher = fetch) {
    this.storage = storage;
    this.sql = storage.sql;
    this.fetcher = fetcher;
    this.refreshing = null;
    this.sql.exec('CREATE TABLE IF NOT EXISTS public_stats (id INTEGER PRIMARY KEY, requests INTEGER NOT NULL, since TEXT NOT NULL, downloads TEXT NOT NULL, downloads_updated TEXT, next_refresh INTEGER NOT NULL)');
    this.sql.exec('INSERT OR IGNORE INTO public_stats VALUES (1, 0, ?, ?, NULL, 0)', new Date(now).toISOString(), '{}');
  }
  recordRequest() {
    this.sql.exec('UPDATE public_stats SET requests = requests + 1 WHERE id = 1');
  }
  async snapshot(now = Date.now()) {
    const row = this.sql.exec('SELECT * FROM public_stats WHERE id = 1').one();
    if (this.refreshing) await this.refreshing;
    else if (row.next_refresh <= now) {
      // Persist the retry deadline before I/O, including failures/restarts. One
      // shared object refreshes GitHub; page views cannot create a refresh storm.
      this.sql.exec('UPDATE public_stats SET next_refresh = ? WHERE id = 1', now + DOWNLOAD_REFRESH_MS);
      this.refreshing = this.refreshDownloads(now).finally(() => { this.refreshing = null; });
      await this.refreshing;
    }
    const current = this.sql.exec('SELECT * FROM public_stats WHERE id = 1').one();
    return {
      downloads: { total: current.downloads_updated ? Object.values(JSON.parse(current.downloads)).reduce((a, b) => a + b, 0) : null,
        updated_at: current.downloads_updated, source: 'github_release_assets' },
      requests: { total: current.requests, since: current.since, updated_at: new Date(now).toISOString() },
      docs: 'https://bailout.dev/docs/#public-stats',
    };
  }
  async refreshDownloads(now) {
    try {
      const observed = await releaseDownloads(this.fetcher);
      this.storage.transactionSync(() => {
        const previous = JSON.parse(this.sql.exec('SELECT downloads FROM public_stats WHERE id = 1').one().downloads);
        // Retain each asset's highest observed count, even if an old release is
        // deleted. These are public asset IDs, not identifiers for people.
        for (const [id, count] of Object.entries(observed)) previous[id] = Math.max(previous[id] ?? 0, count);
        const total = Object.values(previous).reduce((a, b) => a + b, 0);
        if (!Number.isSafeInteger(total)) throw new Error('Invalid total');
        this.sql.exec('UPDATE public_stats SET downloads = ?, downloads_updated = ? WHERE id = 1', JSON.stringify(previous), new Date(now).toISOString());
      });
    } catch { /* Keep the last known value and timestamp; never invent zero. */ }
  }
}

// The canonical cache key ignores query strings and user headers. Counters are
// public and read-only, including while the model service is paused/exhausted.
export async function publicStatsResponse(env, cache = caches.default) {
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
      'Cache-Control': 'public, max-age=60', 'Access-Control-Allow-Origin': 'https://bailout.dev', 'X-Content-Type-Options': 'nosniff' } });
    await cache.put(key, response.clone());
    return browserResponse(response);
  } catch {
    return Response.json({ error: 'Statistics are temporarily unavailable.' }, { status: 503,
      headers: { 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': 'https://bailout.dev' } });
  }
}

function browserResponse(cached) {
  const result = new Response(cached.body, cached);
  // Keep the edge's explicit 60-second Cache API entry, but prevent the zone's
  // longer Browser Cache TTL from freezing counters in a visitor's tab.
  result.headers.set('Cache-Control', 'no-store');
  return result;
}
