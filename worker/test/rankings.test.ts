import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { Rankings, validateManifest } from '../src/rankings.ts';
import { CapacityLedger } from '../src/capacity.ts';
import gateway from '../src/gateway.ts';

function storage() {
  const db = new DatabaseSync(':memory:');
  return { sql: { exec(query, ...args) {
    const s = db.prepare(query), rows = s.columns().length ? s.all(...args) : (s.run(...args), []);
    return { [Symbol.iterator]: () => rows[Symbol.iterator](), one: () => { assert.equal(rows.length, 1); return rows[0]; } };
  } }, transactionSync(fn) { db.exec('BEGIN'); try { const value = fn(); db.exec('COMMIT'); return value; } catch (e) { db.exec('ROLLBACK'); throw e; } } };
}
const now = Date.now();
function manifest(id = 'test/a:free', time = now, run = '1') {
  return { schema: 1, suite: 'bailout-setup-v1', generated_at: new Date(time).toISOString(), run_id: run, harness_sha: 'a'.repeat(40),
    models: [{ id, fingerprint: 'b'.repeat(64), suite: 'bailout-setup-v1', trials: 20, passed: 20, runs: 2,
      critical_failures: 0, native_tools: true, evaluated_at: new Date(time).toISOString() }] };
}

test('ranking publication is atomic, ordered, idempotent and retains untested models', () => {
  const db = storage(), r = new Rankings(db);
  assert.equal(r.snapshot().stale, true);
  r.publish(manifest(), now);
  assert.equal(new Rankings(db).snapshot(now).models.length, 1);
  assert.deepEqual(r.publish(manifest(), now), { ok: true, unchanged: true });
  assert.throws(() => r.publish({ ...manifest(), models: [] }, now));
  assert.throws(() => r.publish(manifest('test/b:free', now - 1000, 'old'), now));
  r.publish(manifest('test/b:free', now + 1, '2'), now + 1);
  assert.equal(r.snapshot(now).models.length, 2);
  assert.equal(r.snapshot(now + 49 * 3600000).stale, true);
  assert.equal(r.snapshot(now + 49 * 3600000).models.length, 2, 'stale refresh never erases the last valid snapshot');
});

test('unknown fields are stripped and malformed evidence never replaces rankings', () => {
  const data = manifest(); data.secret = 'do not retain'; data.models[0].prompt = 'do not retain';
  assert.ok(!JSON.stringify(validateManifest(data, now)).includes('do not retain'));
  for (const patch of [{ passed: 21 }, { trials: 0 }, { runs: 0 }, { runs: 21 }, { fingerprint: 'bad' }, { native_tools: 'true' }, { evaluated_at: 'bad' }, { last_passed: 10 }, { last_trials: 10, last_passed: 11, last_critical_failures: 0 }]) {
    assert.throws(() => validateManifest({ ...manifest(), models: [{ ...manifest().models[0], ...patch }] }, now));
  }
});

test('a newly generated envelope cannot roll a model back to older evidence', () => {
  const r = new Rankings(storage());
  r.publish(manifest(), now);
  const delayed = manifest('test/a:free', now + 10, 'delayed');
  delayed.models[0].evaluated_at = new Date(now - 1000).toISOString();
  assert.throws(() => r.publish(delayed, now + 10));
  assert.equal(r.snapshot(now + 10).run_id, '1');
  const independent = manifest('test/b:free', now + 20, 'independent');
  independent.models[0].evaluated_at = new Date(now - 1000).toISOString();
  r.publish(independent, now + 20);
  assert.equal(r.snapshot(now + 20).models.length, 2);
});

test('verified per-model quotas preserve independent pools but retain shared concurrency and cooldown', () => {
  const c = new CapacityLedger(storage());
  const quota = { rpm: 1, rpd: 100, tpm: 5000, tpd: 10000 };
  const a = 'groq/a:free', b = 'groq/b:free';
  assert.equal(c.reserve('groq', a, 4000, now, quota).ok, true);
  assert.equal(c.reserve('groq', a, 4000, now, quota).scope, 'model');
  assert.equal(c.reserve('groq', b, 4000, now, quota).ok, true);
  assert.equal(c.reserve('groq', 'groq/c:free', 4000, now).ok, false, 'unverified quotas still share the conservative account ceiling');
  c.cooldown('groq', null, 60, now);
  assert.equal(c.reserve('groq', 'groq/d:free', 100, now, quota).scope, 'provider');
  assert.throws(() => c.reserve('openrouter', 'test/a:free', 100, now, quota));
  assert.throws(() => c.reserve('groq', a, 100, now, { ...quota, tpm: 0 }));
});

test('staging binding receives authenticated evaluations only; public traffic stays on API', async () => {
  let evaluationCalls = 0, publicCalls = 0;
  const env = { BENCHMARK_TOKEN: 'e'.repeat(40),
    EDGE_IP_LIMIT: { limit: async () => ({ success: true }) }, EDGE_GLOBAL_LIMIT: { limit: async () => ({ success: true }) },
    BUDGET: { idFromName: s => s, get: () => ({ fetch: async () => Response.json({ ok: true }) }) },
    EVALUATOR: { fetch: async req => { evaluationCalls++; assert.equal(req.headers.get('X-Bailout-Evaluation'), 'authorized'); return Response.json({ candidates: [] }); } },
    API: { fetch: async req => { publicCalls++; assert.equal(req.headers.get('X-Bailout-Evaluation'), null); return Response.json({ ok: true }); } } };
  assert.equal((await gateway.fetch(new Request('https://api.test/internal/bench/catalog', { headers: { Authorization: 'Bearer ' + env.BENCHMARK_TOKEN } }), env)).status, 200);
  assert.equal((await gateway.fetch(new Request('https://api.test/v1/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'CF-Connecting-IP': '192.0.2.1', 'X-Bailout-Evaluation': 'authorized', Authorization: 'Bearer ' + env.BENCHMARK_TOKEN }, body: '{}' }), env)).status, 200);
  assert.equal(evaluationCalls, 1); assert.equal(publicCalls, 1);
});

test('aggregate health survives restarts and does not accept request-specific errors or content', () => {
  const db = storage(), r = new Rankings(db);
  r.record('test/a:free', 'provider_timeout', 40000, now);
  assert.equal(new Rankings(db).snapshot(now).health['test/a:free'].success_ewma, .8);
  r.record('test/a:free', 'success', 1000, now + 1);
  assert.ok(r.snapshot(now + 1).health['test/a:free'].success_ewma > .8);
  const before = JSON.stringify(r.snapshot(now));
  r.record('test/a:free', 'context_exceeded', 0, now);
  r.record('test/a:free', 'prompt text', 0, now);
  assert.equal(JSON.stringify(r.snapshot(now)), before);
});

test('evaluation has a durable daily allowance separate from production quotas', () => {
  const db = storage(); const c = new CapacityLedger(db);
  for (let i = 0; i < 1000; i++) assert.equal(c.benchmark(now).status, 200);
  assert.equal(new CapacityLedger(db).benchmark(now).status, 429);
  assert.equal(c.reserve('openrouter', 'test/a:free', 100, now).ok, true);
});

test('public callers cannot forge evaluation or publish rankings and credentials remain scoped', async () => {
  const evaluationToken = 'e'.repeat(40), publishToken = 'p'.repeat(40);
  let calls = 0;
  const stub = { fetch: async (url, options) => {
    calls++;
    assert.ok(!JSON.stringify(options).includes(evaluationToken));
    assert.ok(!JSON.stringify(options).includes(publishToken));
    return Response.json({ ok: true });
  } };
  const env = { BENCHMARK_TOKEN: evaluationToken, RANKING_PUBLISH_TOKEN: publishToken,
    BUDGET: { idFromName: s => s, get: () => stub }, API: { fetch: async req => {
      assert.equal(req.headers.get('Authorization'), null);
      assert.equal(req.headers.get('X-Bailout-Evaluation'), 'authorized');
      return Response.json({ candidates: [] });
    } } };
  for (const token of ['', 'Bearer bad', 'Bearer ' + publishToken]) {
    const req = new Request('https://api.test/internal/bench/catalog', { headers: { Authorization: token, 'X-Bailout-Evaluation': 'authorized' } });
    assert.equal((await gateway.fetch(req, env)).status, 404);
  }
  assert.equal(calls, 0);
  assert.equal((await gateway.fetch(new Request('https://api.test/internal/bench/catalog', { headers: { Authorization: 'Bearer ' + evaluationToken } }), env)).status, 200);
  assert.equal((await gateway.fetch(new Request('https://api.test/internal/rankings', { method: 'POST', headers: { Authorization: 'Bearer ' + evaluationToken }, body: '{}' }), env)).status, 404);
});
