import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { PublicStats, releaseDownloads, publicStatsResponse, DOWNLOAD_REFRESH_MS, DOWNLOAD_RETRY_MS } from '../src/stats.js';
import { formatCount, statView } from '../../site/stats.js';

function storage() {
  const db = new DatabaseSync(':memory:');
  let alarm = null;
  return { getAlarm: async () => alarm, setAlarm: async when => { alarm = when; }, sql: { exec(query, ...args) {
    const stmt = db.prepare(query);
    const rows = stmt.columns().length ? stmt.all(...args) : (stmt.run(...args), []);
    return { [Symbol.iterator]: () => rows[Symbol.iterator](), one: () => { assert.equal(rows.length, 1); return rows[0]; } };
  } }, transactionSync(fn) {
    db.exec('BEGIN'); try { const result = fn(); db.exec('COMMIT'); return result; } catch (error) { db.exec('ROLLBACK'); throw error; }
  } };
}
const start = Date.UTC(2026, 8, 17, 22);
const asset = (id, download_count, name = 'bailout-linux-x64.tar.gz') => ({ id, download_count, name });

test('download source paginates and counts only supported binaries on public stable releases', async () => {
  const pages = [Array.from({ length: 100 }, () => ({ assets: [] })), [
    { assets: [asset(1, 10), asset(2, 20, 'bailout-macos-arm64.tar.gz'), asset(3, 30, 'bailout-linux-arm64.tar.gz'), asset(4, 500, 'SHA256SUMS'), asset(5, 500, 'source.zip')] },
    { draft: true, assets: [asset(6, 99)] }, { prerelease: true, assets: [asset(7, 99)] },
  ]];
  let calls = 0;
  const result = await releaseDownloads(async (url, options) => {
    assert.equal(new URL(url).searchParams.get('page'), String(++calls));
    assert.equal(options.headers.Authorization, undefined);
    return Response.json(pages[calls - 1]);
  });
  assert.deepEqual(result, { 1: 10, 2: 20, 3: 30 });
  await assert.rejects(releaseDownloads(async () => Response.json([{ assets: [asset(1, -1)] }])));
  await assert.rejects(releaseDownloads(async () => Response.json(Array.from({ length: 100 }, () => ({ assets: [] })))));
});

test('request totals survive restarts; downloads retain deleted assets and never reset on errors', async () => {
  const db = storage(); let calls = 0, fail = false;
  let assets = [asset(1, 10), asset(2, 20)];
  const fetcher = async () => { calls++; return fail ? new Response(null, { status: 429 }) : Response.json([{ assets }]); };
  const stats = new PublicStats(db, start, fetcher);
  for (let i = 0; i < 100; i++) stats.recordRequest();
  await stats.refresh(start);
  let result = stats.snapshot(start);
  assert.equal(result.requests.total, 100); assert.equal(result.downloads.total, 30);
  const restarted = new PublicStats(db, start + 1000, fetcher);
  await restarted.refresh(start + 1000);
  result = restarted.snapshot(start + 1000);
  assert.equal(calls, 1); assert.equal(result.requests.total, 100);
  assert.equal(result.requests.since, new Date(start).toISOString());
  assets = [asset(1, 9), asset(3, 4)];
  await restarted.refresh(start + DOWNLOAD_REFRESH_MS);
  result = restarted.snapshot(start + DOWNLOAD_REFRESH_MS);
  assert.equal(result.downloads.total, 34);
  fail = true;
  await restarted.refresh(start + 2 * DOWNLOAD_REFRESH_MS);
  result = restarted.snapshot(start + 2 * DOWNLOAD_REFRESH_MS);
  assert.equal(result.downloads.total, 34);
  assert.equal(result.downloads.updated_at, new Date(start + DOWNLOAD_REFRESH_MS).toISOString());
  await new PublicStats(db, start, fetcher).refresh(start + 2 * DOWNLOAD_REFRESH_MS + 1);
  assert.equal(calls, 3, 'failed refresh remains backed off across restarts');
});

test('concurrent refreshes share one request; snapshots never wait on GitHub', async () => {
  let calls = 0;
  const stats = new PublicStats(storage(), start, async () => { calls++; throw new Error('offline'); });
  await Promise.all(Array.from({ length: 30 }, () => stats.refresh(start)));
  assert.equal(calls, 1);
  const result = stats.snapshot(start);
  assert.equal(result.downloads.total, null); assert.equal(result.requests.total, 0);
  assert.equal(result.downloads.last_error, 'github_network_error');
});

test('public snapshot cache is canonical and does not invoke admission or copy user data', async () => {
  let calls = 0, cached;
  const cache = { match: async request => { assert.equal(request.url, 'https://api.bailout.dev/v1/stats'); return cached?.clone(); },
    put: async (request, result) => {
      assert.equal(request.url, 'https://api.bailout.dev/v1/stats');
      assert.equal(result.headers.get('Cache-Control'), 'public, max-age=30', 'edge cache stays bounded');
      cached = result;
    } };
  const env = { EDGE_GLOBAL_LIMIT: { limit: async () => ({ success: true }) }, BUDGET: {
    idFromName: name => name, get: id => { assert.equal(id, 'global-v1'); return { fetch: async url => {
      calls++; assert.equal(url, 'https://budget/stats'); return Response.json({ requests: { total: 123 } });
    } }; },
  } };
  for (let i = 0; i < 4; i++) {
    const result = await publicStatsResponse(env, cache);
    assert.equal(result.headers.get('Cache-Control'), 'no-store', 'browser must not retain a stale counter snapshot');
    assert.equal((await result.json()).requests.total, 123);
  }
  assert.equal(calls, 1);
});

test('compact labels use requested units, exact small totals, and honest freshness', () => {
  for (const [value, expected] of [[0,'0'],[999,'999'],[1000,'1k'],[1100,'1.1k'],[11000,'11k'],[101000,'101k'],[999999,'1M'],[1000000,'1M'],[1100000,'1.1M']]) assert.equal(formatCount(value), expected);
  for (const value of [null, undefined, -1, NaN, 2.5]) assert.equal(formatCount(value), '—');
  assert.equal(statView({ total: 10, updated_at: new Date(start).toISOString() }, 'requests', start + 60_000).live, true);
  assert.equal(statView({ total: 10, updated_at: new Date(start).toISOString() }, 'requests', start + 4 * 60_000).live, false);
  assert.equal(statView({ total: null, updated_at: null }, 'downloads', start).live, false);
  assert.equal(statView({ total: 19, updated_at: new Date(start).toISOString() }, 'downloads', start + 4 * 60_000).live, true);
  assert.equal(statView({ total: 19, updated_at: new Date(start).toISOString() }, 'downloads', start + 6 * 60_000).live, false);
});

test('background schedule survives restart, advances unchanged counts, and repairs a missing alarm', async () => {
  const db = storage(); let calls = 0;
  const fetcher = async () => { calls++; return Response.json([{ assets: [asset(1, 19)] }]); };
  const stats = new PublicStats(db, start, fetcher);
  await stats.refresh(start);
  assert.equal(await db.getAlarm(), start + DOWNLOAD_REFRESH_MS);
  await db.setAlarm(null); // Simulate an alarm firing after eviction.
  const restarted = new PublicStats(db, start, fetcher);
  await restarted.refresh(start + DOWNLOAD_REFRESH_MS);
  assert.equal(calls, 2);
  assert.equal(restarted.snapshot().downloads.total, 19);
  assert.equal(restarted.snapshot().downloads.updated_at, new Date(start + DOWNLOAD_REFRESH_MS).toISOString());
  await db.setAlarm(null);
  await restarted.refresh(start + DOWNLOAD_REFRESH_MS + 1000);
  assert.equal(calls, 2);
  assert.equal(await db.getAlarm(), start + 2 * DOWNLOAD_REFRESH_MS);
});

test('a transient failure retries promptly, preserves the count, and then restores freshness', async () => {
  const db = storage(); let fail = false;
  const stats = new PublicStats(db, start, async () => fail ? new Response('private upstream body', { status: 502 }) : Response.json([{ assets: [asset(1, 19)] }]));
  await stats.refresh(start);
  fail = true;
  await stats.refresh(start + DOWNLOAD_REFRESH_MS);
  const stale = stats.snapshot(start + DOWNLOAD_REFRESH_MS);
  assert.equal(stale.downloads.total, 19);
  assert.equal(stale.downloads.updated_at, new Date(start).toISOString());
  assert.equal(stale.downloads.last_error, 'github_http_502');
  assert.equal(stale.downloads.consecutive_failures, 1);
  assert.equal(await db.getAlarm(), start + DOWNLOAD_REFRESH_MS + DOWNLOAD_RETRY_MS);
  assert.ok(!JSON.stringify(stale).includes('private upstream body'));
  fail = false;
  await stats.refresh(start + DOWNLOAD_REFRESH_MS + DOWNLOAD_RETRY_MS);
  assert.equal(stats.snapshot().downloads.last_error, null);
  assert.equal(stats.snapshot().downloads.consecutive_failures, 0);
  assert.equal(stats.snapshot().downloads.updated_at, new Date(start + DOWNLOAD_REFRESH_MS + DOWNLOAD_RETRY_MS).toISOString());
});

test('GitHub rate limits survive restarts and page traffic cannot bypass Retry-After', async () => {
  const db = storage(); let calls = 0;
  const reset = start + 3600_000;
  const fetcher = async () => { calls++; return new Response('secret', { status: 403, headers: {
    'Retry-After': '120', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': String(reset / 1000),
  } }); };
  const stats = new PublicStats(db, start, fetcher);
  await stats.refresh(start);
  assert.equal(await db.getAlarm(), reset + 1000);
  const restarted = new PublicStats(db, start, fetcher);
  for (let i = 1; i < 20; i++) await restarted.refresh(start + i * 60_000);
  assert.equal(calls, 1);
  assert.equal(restarted.snapshot().downloads.last_error, 'github_http_403');
  await restarted.refresh(reset + 1000);
  assert.equal(calls, 2);
});

test('the old fifteen-minute schedule upgrades without losing stored totals', async () => {
  const db = storage();
  const stats = new PublicStats(db, start, async () => Response.json([{ assets: [asset(1, 20)] }]));
  db.sql.exec('UPDATE public_stats SET downloads = ?, downloads_updated = ?, next_refresh = ? WHERE id = 1', '{"1":13}', new Date(start).toISOString(), start + 900_000);
  stats.recordRequest();
  await stats.refresh(start + 1000);
  assert.equal(stats.snapshot().downloads.total, 20);
  assert.equal(stats.snapshot().requests.total, 1);
  assert.equal(await db.getAlarm(), start + 1000 + DOWNLOAD_REFRESH_MS);
});

test('partial, malformed and stalled refreshes never publish an incomplete total', async () => {
  let page = 0;
  await assert.rejects(releaseDownloads(async () => ++page === 1 ? Response.json(Array.from({ length: 100 }, () => ({ assets: [asset(1, 30)] }))) : new Response(null, { status: 503 })), /github_http_503/);
  await assert.rejects(releaseDownloads(async () => new Response('invalid JSON')), /github_invalid_response/);
  await assert.rejects(releaseDownloads(async () => Response.json([null])), /github_invalid_response/);
  let unblock;
  const stats = new PublicStats(storage(), start, async () => new Promise(resolve => { unblock = resolve; }));
  const pending = stats.refresh(start);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stats.snapshot().downloads.total, null, 'a slow source does not block snapshots');
  stats.recordRequest();
  assert.equal(stats.snapshot().requests.total, 1, 'other operations still run during refresh');
  unblock(Response.json([{ assets: [asset(1, 19)] }]));
  await pending;
  assert.equal(stats.snapshot().downloads.total, 19);
});

test('dedicated GitHub authentication stays on the fixed source and out of statistics', async () => {
  const db = storage();
  const token = 'test-only-private-value';
  const stats = new PublicStats(db, start, async (url, options) => {
    assert.equal(new URL(url).origin, 'https://api.github.com');
    assert.equal(new URL(url).pathname, '/repos/storozhenko98/bailout/releases');
    assert.equal(options.headers.Authorization, `Bearer ${token}`);
    assert.equal(options.redirect, 'manual', 'credentials must never follow a redirect');
    return Response.json([{ assets: [asset(1, 19)] }]);
  }, token);
  await stats.refresh(start);
  assert.equal(stats.snapshot().downloads.total, 19);
  for (const table of ['public_stats', 'download_refresh', 'download_source']) {
    assert.ok(!JSON.stringify([...db.sql.exec(`SELECT * FROM ${table}`)]).includes(token));
  }
  assert.ok(!JSON.stringify(stats.snapshot()).includes(token));
});

test('provisioning authentication replaces the anonymous quota without waiting for its reset', async () => {
  const db = storage();
  const anonymous = new PublicStats(db, start, async () => new Response(null, { status: 403, headers: { 'Retry-After': '3600' } }));
  await anonymous.refresh(start);
  assert.equal(await db.getAlarm(), start + 3600_000);
  const authenticated = new PublicStats(db, start, async () => Response.json([{ assets: [asset(1, 19)] }]), 'test-only-token');
  await authenticated.refresh(start + 1000);
  assert.equal(authenticated.snapshot().downloads.total, 19);
  assert.equal(authenticated.snapshot().downloads.last_error, null);
  assert.equal(await db.getAlarm(), start + 1000 + DOWNLOAD_REFRESH_MS);
});
