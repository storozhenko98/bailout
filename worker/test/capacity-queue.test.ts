import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { CapacityLedger, WAITING, type QueueRoute } from '../src/capacity.ts';
import { buildSync } from 'esbuild';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

function ledger() {
  const db = new DatabaseSync(':memory:');
  const storage = { sql: { exec(query, ...args) {
    const stmt = db.prepare(query);
    const rows = stmt.columns().length ? stmt.all(...args) : (stmt.run(...args), []);
    return { [Symbol.iterator]: () => rows[Symbol.iterator]() };
  } }, transactionSync(fn) {
    db.exec('BEGIN'); try { const result = fn(); db.exec('COMMIT'); return result; }
    catch (error) { db.exec('ROLLBACK'); throw error; }
  } };
  return { capacity: new CapacityLedger(storage), storage };
}
const now = Date.UTC(2026, 8, 18, 22);
const route = (provider = 'zai', model = 'zai/flash:free', tokens = 100): QueueRoute => ({ provider, model, tokens });
const id = () => crypto.randomUUID();
const take = (c: CapacityLedger, ticket: string, r = route(), at = now) => c.reserve(r.provider, r.model, r.tokens, at, r.quota, ticket);

test('oldest compatible waiter wins after release, including before a fast follow-up', () => {
  const { capacity: c, storage } = ledger();
  const active = c.reserve('zai', route().model, 100, now);
  const a = id(), b = id(), followup = id();
  c.join(a, [route()], now); c.join(b, [route()], now);
  assert.equal(c.peek(a, now).ok, false);
  c.settle(active.permit!, 100);
  // Reconstructing the object must preserve order.
  const restarted = new CapacityLedger(storage);
  assert.equal(take(restarted, b).code, 'queue_wait');
  const first = take(restarted, a); assert.equal(first.ok, true);
  restarted.settle(first.permit!, 100);
  restarted.join(followup, [route()], now);
  assert.equal(take(restarted, followup).code, 'queue_wait');
  assert.equal(take(restarted, b).ok, true);
});

test('unavailable older routes do not block siblings, independent pools, or fitting contexts', () => {
  const { capacity: c } = ledger();
  const a = id(), b = id();
  c.cooldown('zai', route().model, 60, now);
  c.join(a, [route()], now);
  c.join(b, [route(), route('openrouter', 'test/a:free')], now);
  assert.equal(take(c, b, route('openrouter', 'test/a:free')).ok, true);
  const large = id(), small = id();
  c.join(large, [route('groq', 'groq/a:free', 8000)], now);
  c.join(small, [route('groq', 'groq/a:free', 100)], now);
  assert.equal(take(c, small, route('groq', 'groq/a:free', 100)).ok, true);
  const sibling = id();
  c.join(sibling, [route('zai', 'zai/other:free')], now);
  assert.equal(take(c, sibling, route('zai', 'zai/other:free')).ok, true);
});

test('earlier request can use a sibling route sharing the same provider quota', () => {
  const { capacity: c } = ledger();
  const a = id(), b = id();
  c.join(a, [route('openrouter', 'test/a:free')], now);
  c.join(b, [route('openrouter', 'test/b:free')], now);
  assert.equal(take(c, b, route('openrouter', 'test/b:free')).code, 'queue_wait');
  assert.equal(take(c, a, route('openrouter', 'test/a:free')).ok, true);
  assert.equal(take(c, b, route('openrouter', 'test/b:free')).ok, true, 'inference remains concurrent after admission');
});

test('abandoned tickets expire, cancellation releases immediately, heartbeats cannot wait forever', () => {
  const { capacity: c } = ledger();
  const a = id(), b = id();
  c.join(a, [route()], now); c.join(b, [route()], now);
  c.leave(a);
  assert.equal(c.peek(b, now).ok, true);
  assert.equal(c.peek(b, now + WAITING.leaseMs).code, 'queue_expired');
  const heartbeat = id(); c.join(heartbeat, [route()], now);
  for (let offset = 10000; offset < WAITING.lifetimeMs; offset += 10000) assert.equal(c.peek(heartbeat, now + offset).ok, true);
  assert.equal(c.peek(heartbeat, now + WAITING.lifetimeMs).code, 'queue_expired');
  assert.equal(take(c, id(), route(), now + WAITING.lifetimeMs).code, 'queue_expired');
});

test('bounded idempotent queue does not reset priority, store extra fields, or reserve tokens by polling', () => {
  const { capacity: c, storage } = ledger();
  const tickets = Array.from({length: WAITING.maximum}, id);
  tickets.forEach(ticket => assert.equal(c.join(ticket, [{...route(), prompt: 'must not persist'} as QueueRoute], now).ok, true));
  assert.equal(c.join(id(), [route()], now).code, 'queue_full');
  assert.equal(c.join(tickets[0], [route()], now + 1).ok, true);
  assert.equal(c.peek(tickets[0], now + 1).ok, true);
  assert.equal(c.peek(tickets[1], now + 1).ok, false);
  assert.equal([...storage.sql.exec('SELECT * FROM inference_attempts')].length, 0);
  assert.ok([...storage.sql.exec('SELECT routes FROM inference_waiters')].every(row => !row.routes.includes('prompt')));
  c.leave(tickets[0]);
  assert.equal(c.join(id(), [route()], now + 1).ok, true);
  assert.throws(() => c.join(id(), [route('unknown')], now));
});

test('legacy and benchmark callers yield, and joining never overrides quota reservations', () => {
  const { capacity: c } = ledger();
  const queued = id(); c.join(queued, [route()], now);
  assert.equal(c.reserve('zai', route().model, 100, now).code, 'queue_wait');
  c.leave(queued);
  for (let i = 0; i < 18; i++) {
    const permit = c.reserve('openrouter', 'test/a:free', 100, now);
    assert.equal(permit.ok, true); c.settle(permit.permit!, 50);
  }
  c.join(queued, [route('openrouter', 'test/a:free')], now);
  assert.equal(take(c, queued, route('openrouter', 'test/a:free')).retry_after_seconds, 60);
  assert.equal(c.peek(queued, now).retry_after_seconds, 60);
});

test('checking a full queue reads shared provider usage only once per poll', () => {
  const { capacity: c, storage } = ledger();
  c.reserve('openrouter', 'test/busy:free', 100, now);
  c.reserve('openrouter', 'test/busy:free', 100, now);
  for (let i=0;i<WAITING.maximum-1;i++) c.join(id(), [route('openrouter','test/busy:free',100+i)], now);
  const fresh=id(); c.join(fresh,[route('openrouter','test/available:free')],now);
  const exec=storage.sql.exec;
  let reads=0;
  storage.sql.exec=(query,...args) => { if (query.startsWith('SELECT started, tokens')) reads++; return exec(query,...args); };
  assert.equal(c.peek(fresh,now).ok,true);
  assert.equal(reads,1);
});

test('real Workers SQL preserves FIFO when waiting clients race in reverse order', async () => {
  const script = buildSync({ stdin: { contents: `import { CapacityLedger } from './src/capacity.ts';
    export class Queue { constructor(state) { this.c = new CapacityLedger(state.storage); }
      async fetch(request) { const {action, args} = await request.json(); return Response.json(this.c[action](...args) ?? {}); } }
    export default { fetch() { return new Response('private'); } };`, resolveDir: process.cwd() },
    bundle:true, write:false, format:'esm', platform:'browser' }).outputFiles[0].text;
  const mf = new Miniflare(convertV4MiniflareOptions({ workers:[{ name:'queue', modules:[{type:'ESModule',path:'queue.js',contents:script}], compatibilityDate:'2026-09-17', durableObjects:{ QUEUE:{className:'Queue', useSQLite:true} } }] }));
  try {
    const ns = await mf.getDurableObjectNamespace('QUEUE');
    const stub = ns.get(ns.idFromName('test'));
    const call = (action, ...args) => stub.fetch('https://queue/', {method:'POST',body:JSON.stringify({action,args})}).then(r=>r.json());
    const tickets=Array.from({length:10},id);
    for (const ticket of tickets) await call('join',ticket,[route()],now);
    for (let index=0;index<tickets.length;index++) {
      const attempts=await Promise.all(tickets.slice(index).reverse().map(async ticket => ({ticket,result:await call('reserve','zai',route().model,100,now,undefined,ticket)})));
      const admitted=attempts.filter(({result})=>result.ok);
      assert.equal(admitted.length,1);
      assert.equal(admitted[0].ticket,tickets[index]);
      await call('settle',admitted[0].result.permit,100);
    }
  } finally { await mf.dispose(); }
});
