import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { BudgetLedger, POLICY } from '../src/budget.js';
import gateway, { clientNetwork } from '../src/gateway.js';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { fileURLToPath } from 'node:url';

function ledger(allowance) {
  const db = new DatabaseSync(':memory:');
  const storage = { sql: { exec(query, ...args) {
    const stmt = db.prepare(query);
    const rows = stmt.columns().length ? stmt.all(...args) : (stmt.run(...args), []);
    return { [Symbol.iterator]: () => rows[Symbol.iterator](), one: () => { assert.equal(rows.length, 1); return rows[0]; } };
  } }, transactionSync(fn) {
    db.exec('BEGIN'); try { const r = fn(); db.exec('COMMIT'); return r; } catch (e) { db.exec('ROLLBACK'); throw e; }
  } };
  return { storage, gate: new BudgetLedger(storage, allowance) };
}
const client = 'a'.repeat(64), now = Date.UTC(2026, 8, 17, 12);

test('reservation happens before admission, persists across restart and fails at exact boundary', () => {
  const { gate, storage } = ledger(240);
  assert.equal(gate.admit(client, 'chat', now).status, 200);
  assert.equal(new BudgetLedger(storage, 240).admit(client, 'chat', now).status, 200);
  const denied = gate.admit(client, 'chat', now);
  assert.equal(denied.status, 503);
  assert.equal(denied.body.code, 'budget_exhausted');
  assert.match(denied.body.error, /hosting allowance is exhausted/);
  assert.equal(gate.admit(client, 'chat', Date.UTC(2026, 9, 1)).status, 503, 'calendar rollover must not double the budget');
  assert.equal(gate.admit(client, 'chat', now + 745 * 3600000).status, 200);
});

test('all model, status and docs admissions share the allowance; rejected attempts cost reserves', () => {
  const { gate } = ledger(250);
  assert.equal(gate.admit(client, 'read', now).status, 200);
  assert.equal(gate.admit(client, 'chat', now).status, 200);
  assert.equal(gate.admit(client, 'status', now).status, 200);
  assert.equal(gate.admit(client, 'status', now).body.code, 'budget_exhausted');
});

test('global inference quota is shared across unrelated clients', () => {
  const { gate } = ledger();
  for (let i = 0; i < POLICY.chatMinute; i++) assert.equal(gate.admit(i.toString(16).padStart(64, '0'), 'chat', now).status, 200);
  assert.equal(gate.admit(client, 'chat', now).body.code, 'capacity_busy');
  assert.equal(gate.admit(client, 'chat', now + 60000).status, 200);
});

test('client minute, hour and day limits survive time and object changes', () => {
  const { gate, storage } = ledger();
  for (let i = 0; i < 30; i++) assert.equal(gate.admit(client, 'read', now).status, 200);
  let r = new BudgetLedger(storage).admit(client, 'read', now);
  assert.equal(r.body.code, 'client_rate_limited'); assert.equal(r.body.retry_after_seconds, 60);
  for (let i = 1; i < 10; i++) for (let j = 0; j < 30; j++) assert.equal(gate.admit(client, 'read', now + i * 60000).status, 200);
  r = gate.admit(client, 'read', now + 10 * 60000); assert.equal(r.body.code, 'client_rate_limited'); assert.match(r.body.error, /hour/);
  for (let hour = 1; hour < 4; hour++) for (let i = 0; i < (hour === 3 ? 100 : 300); i++) assert.equal(gate.admit(client, 'read', now + hour * 3600000 + Math.floor(i / 30) * 60000).status, 200);
  r = gate.admit(client, 'read', now + 4 * 3600000); assert.equal(r.body.code, 'client_rate_limited'); assert.match(r.body.error, /daily/);
});

test('IPv6 addresses in a /64 and mapped IPv4 cannot evade identity grouping', () => {
  assert.equal(clientNetwork('2001:db8:1234:abcd::1'), clientNetwork('2001:db8:1234:abcd:ffff:abcd::2'));
  assert.equal(clientNetwork('::ffff:192.0.2.1'), clientNetwork('192.0.2.1'));
  for (const bad of ['', 'local', '300.1.1.1', 'abc:zz']) assert.throws(() => clientNetwork(bad));
});

function runtime(allowance, api) {
  return new Miniflare(convertV4MiniflareOptions({ workers: [{ name: 'gateway', modules: ['gateway.js', 'budget.js'].map(name => ({type:'ESModule', path:fileURLToPath(new URL('../src/' + name, import.meta.url))})), compatibilityDate: '2026-09-17',
    bindings: { BUDGET_ALLOWANCE_MICRO_USD: String(allowance), SERVICE_PAUSED: 'false' },
    durableObjects: { BUDGET: { className: 'BudgetGuard', useSQLite: true } },
    ratelimits: { EDGE_IP_LIMIT: { namespace_id: '1005', simple: { limit: 60, period: 60 } }, EDGE_GLOBAL_LIMIT: { namespace_id: '1006', simple: { limit: 240, period: 60 } } },
    serviceBindings: { API: api },
  }] }));
}

test('real Workers runtime serializes concurrent reservations; denied calls never reach backend', async () => {
  let upstream = 0;
  const mf = runtime(240, async req => { upstream++; assert.equal(req.headers.get('Authorization'), null); return Response.json({ ok: true }); });
  try {
    const responses = await Promise.all(Array.from({ length: 12 }, (_, i) => mf.dispatchFetch('https://api.test/v1/chat', { method: 'POST', headers: { 'CF-Connecting-IP': `192.0.2.${i + 1}`, 'Content-Type': 'application/json', Authorization: 'untrusted' }, body: '{"messages":[]}' })));
    assert.equal(responses.filter(r => r.status === 200).length, 2);
    assert.equal(upstream, 2);
    for (const r of responses.filter(r => r.status !== 200)) { assert.equal(r.status, 503); assert.ok(Number(r.headers.get('Retry-After')) > 0); assert.equal((await r.json()).code, 'budget_exhausted'); }
    assert.equal((await mf.dispatchFetch('https://api.test/health')).status, 200);
  } finally { await mf.dispose(); }
});

test('unknown routes, malformed identity, oversized chunked bodies, pause, and ledger failure never invoke Python', async () => {
  let upstream = 0;
  const mf = runtime(35000000, async () => { upstream++; return new Response('unexpected'); });
  const opts = { method: 'POST', headers: { 'CF-Connecting-IP': '192.0.2.99', 'Content-Type': 'application/json' } };
  try {
    assert.equal((await mf.dispatchFetch('https://api.test/evil')).status, 404);
    assert.equal((await mf.dispatchFetch('https://api.test/v1/chat')).status, 405);
    const huge = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(512001)); c.close(); } });
    assert.equal((await mf.dispatchFetch('https://api.test/v1/chat', { ...opts, body: huge, duplex: 'half' })).status, 413);
  } finally { await mf.dispose(); }
  assert.equal(upstream, 0);
});

test('manual pause and unavailable capacity checks fail closed with structured errors', async () => {
  const request = new Request('https://api.test/v1/models', {headers:{'CF-Connecting-IP':'192.0.2.1'}});
  const paused = await gateway.fetch(request, {SERVICE_PAUSED:'true'});
  assert.equal((await paused.json()).code, 'service_paused');
  const broken = await gateway.fetch(request, {});
  assert.equal(broken.status, 503);
  assert.equal((await broken.json()).code, 'service_unavailable');
});

test('deployed limits and service bindings preserve the reservation assumptions', () => {
  const api = JSON.parse(readFileSync(new URL('../../api/wrangler.jsonc', import.meta.url)));
  const gateway = JSON.parse(readFileSync(new URL('../gateway.wrangler.jsonc', import.meta.url)));
  const site = JSON.parse(readFileSync(new URL('../wrangler.jsonc', import.meta.url)));
  assert.equal(api.limits.cpu_ms, 5000);
  assert.equal(api.workers_dev, false); assert.equal(api.preview_urls, false); assert.deepEqual(api.routes, []);
  assert.equal(gateway.workers_dev, false); assert.equal(gateway.preview_urls, false);
  assert.equal(Number(gateway.vars.BUDGET_ALLOWANCE_MICRO_USD), POLICY.allowanceMicroUsd);
  assert.equal(gateway.services[0].service, api.name); assert.equal(site.services[0].service, gateway.name);
  assert.equal(site.limits.cpu_ms, 50); assert.equal(gateway.limits.cpu_ms, 50);
  assert.ok(POLICY.forwardMicroUsd > (api.limits.cpu_ms + site.limits.cpu_ms + gateway.limits.cpu_ms) * .02);
});
