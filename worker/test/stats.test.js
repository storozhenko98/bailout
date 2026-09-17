import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { PublicStats, releaseDownloads, publicStatsResponse, DOWNLOAD_REFRESH_MS } from '../src/stats.js';
import { formatCount, statView } from '../../site/stats.js';

function storage() {
  const db = new DatabaseSync(':memory:');
  return { sql: { exec(query, ...args) {
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
  let result = await stats.snapshot(start);
  assert.equal(result.requests.total, 100); assert.equal(result.downloads.total, 30);
  const restarted = new PublicStats(db, start + 1000, fetcher);
  result = await restarted.snapshot(start + 1000);
  assert.equal(calls, 1); assert.equal(result.requests.total, 100);
  assert.equal(result.requests.since, new Date(start).toISOString());
  assets = [asset(1, 9), asset(3, 4)];
  result = await restarted.snapshot(start + DOWNLOAD_REFRESH_MS);
  assert.equal(result.downloads.total, 34);
  fail = true;
  result = await restarted.snapshot(start + 2 * DOWNLOAD_REFRESH_MS);
  assert.equal(result.downloads.total, 34);
  assert.equal(result.downloads.updated_at, new Date(start + DOWNLOAD_REFRESH_MS).toISOString());
  await new PublicStats(db, start, fetcher).snapshot(start + 2 * DOWNLOAD_REFRESH_MS + 1);
  assert.equal(calls, 3, 'failed refresh remains backed off across restarts');
});

test('cold concurrent snapshots share one download refresh; unavailable is not a fake zero', async () => {
  let calls = 0;
  const stats = new PublicStats(storage(), start, async () => { calls++; throw new Error('offline'); });
  const results = await Promise.all(Array.from({ length: 30 }, () => stats.snapshot(start)));
  assert.equal(calls, 1);
  for (const result of results) { assert.equal(result.downloads.total, null); assert.equal(result.requests.total, 0); }
});

test('public snapshot cache is canonical and does not invoke admission or copy user data', async () => {
  let calls = 0, cached;
  const cache = { match: async request => { assert.equal(request.url, 'https://api.bailout.dev/v1/stats'); return cached?.clone(); },
    put: async (request, result) => { assert.equal(request.url, 'https://api.bailout.dev/v1/stats'); cached = result; } };
  const env = { EDGE_GLOBAL_LIMIT: { limit: async () => ({ success: true }) }, BUDGET: {
    idFromName: name => name, get: id => { assert.equal(id, 'global-v1'); return { fetch: async url => {
      calls++; assert.equal(url, 'https://budget/stats'); return Response.json({ requests: { total: 123 } });
    } }; },
  } };
  for (let i = 0; i < 4; i++) {
    const result = await publicStatsResponse(env, cache);
    assert.equal(result.headers.get('Cache-Control'), 'public, max-age=60');
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
});
