import type { Env, HttpResult } from "./types.js";
import { BudgetLedger, POLICY, budgetRefusal, refusal } from './budget.js';
import { PublicStats, publicStatsResponse } from './stats.js';
import { CapacityLedger, PROVIDERS, type QueueRoute } from './capacity.js';
import { Rankings } from './rankings.js';

export class BudgetGuard {
  rankings: Rankings; ctx: DurableObjectState; ledger: BudgetLedger; capacity: CapacityLedger; providers: string[]; stats: PublicStats;
  constructor(ctx: DurableObjectState, env: Env) {
    this.ctx = ctx;
    this.ledger = new BudgetLedger(ctx.storage, Number(env.BUDGET_ALLOWANCE_MICRO_USD), Number(env.CHAT_ADMISSIONS_PER_MINUTE || POLICY.chatMinute));
    this.capacity = new CapacityLedger(ctx.storage);
    this.rankings = new Rankings(ctx.storage);
    this.providers = (env.PROVIDER_POOL || 'openrouter').split(',').filter(p => Object.hasOwn(PROVIDERS, p));
    this.stats = new PublicStats(ctx.storage, Date.now(), fetch, env.GITHUB_STATS_TOKEN);
  }
  async fetch(request: Request) {
    const path = new URL(request.url).pathname;
    // This object has no public HTTP route. Only the private API service can
    // reserve provider attempts; gateway allowlists reject these paths.
    if (path === '/rankings/publish' && request.method === 'POST') {
      try { return Response.json(this.rankings.publish(await request.json())); }
      catch { return Response.json({ error: 'Invalid or older ranking snapshot.' }, { status: 400 }); }
    }
    if (path.startsWith('/capacity/') && request.method === 'POST') {
      const data = await request.json() as { provider: string; model: string | null; tokens: number; permit: string; seconds: number; outcome: string; latency_ms: number; ticket?: string; routes: QueueRoute[]; quota?: { rpm: number; rpd: number; tpm: number; tpd: number } };
      if (path === '/capacity/benchmark') return response(this.capacity.benchmark());
      if (path === '/capacity/rankings') return Response.json(this.rankings.snapshot());
      if (path === '/capacity/outcome') { this.rankings.record(data.model!, data.outcome, data.latency_ms); return Response.json({ ok: true }); }
      if (path === '/capacity/join') return Response.json(this.capacity.join(data.ticket!, data.routes));
      if (path === '/capacity/peek') return Response.json(this.capacity.peek(data.ticket!));
      if (path === '/capacity/leave') { this.capacity.leave(data.ticket!); return Response.json({ ok: true }); }
      if (path === '/capacity/reserve') return Response.json(this.capacity.reserve(data.provider, data.model!, data.tokens, Date.now(), data.quota, data.ticket));
      if (path === '/capacity/settle') this.capacity.settle(data.permit, data.tokens);
      else if (path === '/capacity/cooldown') this.capacity.cooldown(data.provider, data.model, data.seconds);
      else return new Response(null, { status: 404 });
      return Response.json({ ok: true });
    }
    if (path === '/stats' && request.method === 'GET') {
      // Serve the snapshot immediately; a slow GitHub request must not make
      // either counter disappear or block the shared admission object.
      this.ctx.waitUntil(this.stats.refresh());
      return Response.json(this.stats.snapshot());
    }
    if (path === '/record-request' && request.method === 'POST') {
      this.stats.recordRequest();
      return new Response(null, { status: 204 });
    }
    if (path !== '/admit' || request.method !== 'POST') return new Response(null, { status: 404 });
    const { client, kind } = await request.json() as { client: string; kind: string };
    const result = this.ledger.admit(client, kind);
    if (kind === 'status' && result.status === 200) {
      result.body.provider_limits = Object.fromEntries(this.providers.map(p => [p, PROVIDERS[p]]));
      const ranking = this.rankings.snapshot();
      result.body.ranking = { updated_at: ranking.generated_at ?? null, stale: ranking.stale, evaluated_models: ranking.models.length };
    }
    return response(result);
  }
  async alarm() { await this.stats.refresh(); }
}

function response({ status, body }: HttpResult) {
  const headers: Record<string, string> = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Access-Control-Allow-Origin': 'https://bailout.dev' };
  if (body.retry_after_seconds) headers['Retry-After'] = String(body.retry_after_seconds);
  return Response.json(body, { status, headers });
}

// IPv6 /64 grouping prevents trivially rotating addresses within one allocation.
export function clientNetwork(ip: string) {
  if (/^(\d{1,3}\.){3}\d{1,3}$/.test(ip)) {
    const parts = ip.split('.').map(Number);
    if (parts.every(x => x <= 255)) return parts.join('.');
  }
  if (ip.includes(':')) {
    try {
      const canonical = new URL(`http://[${ip}]/`).hostname.slice(1, -1);
      const [left, right = ''] = canonical.split('::');
      const a = left ? left.split(':') : [], b = right ? right.split(':') : [];
      const parts = [...a, ...Array(8 - a.length - b.length).fill('0'), ...b].map(x => parseInt(x, 16));
      if (parts.slice(0, 5).every(x => x === 0) && parts[5] === 65535) return `${parts[6] >> 8}.${parts[6] & 255}.${parts[7] >> 8}.${parts[7] & 255}`;
      return parts.slice(0, 4).map(x => x.toString(16)).join(':') + '::/64';
    } catch { /* reject missing or malformed platform-provided identity */ }
  }
  throw new Error('Missing trusted client IP');
}

let budgetClosedUntil = 0;
let budgetReset = 0;
export default {
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith('/internal/')) return internal(request, env);
    if (url.pathname === '/v1/stats') {
      if (request.method !== 'GET') return response({ status: 405, body: { error: 'Method not allowed.', code: 'method_not_allowed' } });
      return publicStatsResponse(env);
    }
    if (request.method === 'GET' && (url.pathname === '/' || url.pathname === '/health')) {
      return response({ status: 200, body: { ok: true, service: 'bailout', version: '0.7.0', free_only: true,
        framework: 'FastAPI', status: '/v1/status', docs: 'https://bailout.dev/docs/#service-limits' } });
    }
    const isChat = url.pathname === '/v1/chat';
    const known = isChat || ['/v1/models', '/v1/status', '/docs', '/openapi.json'].includes(url.pathname);
    if (!known) return response({ status: 404, body: { error: 'Not found.', code: 'not_found' } });
    if (request.method !== (isChat ? 'POST' : 'GET')) return response({ status: 405, body: { error: 'Method not allowed.', code: 'method_not_allowed' } });
    if (env.SERVICE_PAUSED === 'true') return response(refusal('service_paused', 'Bailout is temporarily paused by its operator. Try again later. See https://bailout.dev/docs/#service-limits', 3600, 503));
    if (Date.now() < budgetClosedUntil) return response(budgetRefusal(budgetReset));
    try {
      const network = clientNetwork(request.headers.get('CF-Connecting-IP') || '');
      const edge = await env.EDGE_IP_LIMIT.limit({ key: network });
      if (!edge.success) return response(refusal('client_rate_limited', 'Too many requests from this IP address. Wait a minute before retrying.'));
      const burst = await env.EDGE_GLOBAL_LIMIT.limit({ key: 'all' });
      if (!burst.success) return response(refusal('capacity_busy', 'Shared free capacity is busy. Wait a minute before retrying.'));
      // Rotate stored pseudonyms daily. Neither the IP nor request body is stored.
      const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${Math.floor(Date.now() / 86400000)}:${network}`));
      const client = [...new Uint8Array(hash)].map(x => x.toString(16).padStart(2, '0')).join('');
      const stub = env.BUDGET.get(env.BUDGET.idFromName('global-v1'));
      const check = await stub.fetch('https://budget/admit', { method: 'POST', body: JSON.stringify({ client, kind: isChat ? 'chat' : url.pathname === '/v1/status' ? 'status' : 'read' }) });
      if (!check.ok) {
        const body = await check.json() as Record<string, unknown>;
        if (body.code === 'budget_exhausted') {
          budgetReset = Date.parse(String(body.resets_at));
          budgetClosedUntil = Math.min(Date.now() + 60_000, budgetReset);
        }
        return response({ status: check.status, body });
      }
      if (url.pathname === '/v1/status') return check;
      let body;
      if (isChat) {
        if (!(request.headers.get('Content-Type') || '').toLowerCase().startsWith('application/json')) return response({ status: 415, body: { error: 'Use application/json.', code: 'invalid_content_type' } });
        if (Number(request.headers.get('Content-Length')) > 4_000_000) return response({ status: 413, body: { error: 'Conversation exceeds the 4 MB transport limit. History is preserved; use /new.', code: 'body_too_large' } });
        const reader = request.body?.getReader();
        if (!reader) return response({ status: 400, body: { error: 'Missing request body.', code: 'invalid_body' } });
        const chunks = []; let size = 0;
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > 4_000_000) { await reader.cancel(); return response({ status: 413, body: { error: 'Conversation exceeds the 4 MB transport limit. History is preserved; use /new.', code: 'body_too_large' } }); }
            chunks.push(value);
          }
        } finally { reader.releaseLock(); }
        body = new Blob(chunks);
      }
      // Python has no public route or workers.dev URL. Every compatibility URL
      // also enters this gateway; caller-supplied forwarding/auth headers vanish.
      const headers = new Headers({ 'Content-Type': 'application/json' });
      const upstream = await env.API.fetch(new Request(`https://api.internal${url.pathname}`, { method: request.method, headers, body, signal: request.signal }));
      if (isChat && upstream.ok) {
        // Only a successful backend acceptance counts, once per HTTP request.
        // Streaming can still fail later. No user data reaches the counter.
        try { await stub.fetch('https://budget/record-request', { method: 'POST' }); }
        catch { /* Statistics must not interrupt a model response. */ }
      }
      const result = new Response(upstream.body, upstream);
      result.headers.set('Cache-Control', 'no-store');
      result.headers.set('Access-Control-Allow-Origin', 'https://bailout.dev');
      return result;
    } catch {
      return response(refusal('service_unavailable', 'Bailout could not verify its capacity or reach its backend. No new work will be accepted until that check succeeds.', 60, 503));
    }
  },
};

async function authorized(request: Request, secret: string | undefined) {
  if (!secret || secret.length < 32) return false;
  const value = request.headers.get('Authorization') || '';
  if (value.length > 1024) return false;
  const [a, b] = await Promise.all([value, 'Bearer ' + secret].map(s => crypto.subtle.digest('SHA-256', new TextEncoder().encode(s))));
  const x = new Uint8Array(a), y = new Uint8Array(b);
  return x.reduce((diff, byte, i) => diff | (byte ^ y[i]), 0) === 0;
}

async function internal(request: Request, env: Env) {
  const path = new URL(request.url).pathname;
  const publish = path === '/internal/rankings' && request.method === 'POST';
  const evaluation = (path === '/internal/bench/catalog' && request.method === 'GET') || (path === '/internal/bench/chat' && request.method === 'POST');
  if ((!publish && !evaluation) || !await authorized(request, publish ? env.RANKING_PUBLISH_TOKEN : env.BENCHMARK_TOKEN))
    return response({ status: 404, body: { error: 'Not found.' } });
  const gate = env.BUDGET.get(env.BUDGET.idFromName('global-v1'));
  if (publish) {
    const raw = await boundedBody(request, 128000);
    if (!raw) return response({ status: 413, body: { error: 'Manifest too large.' } });
    return gate.fetch('https://budget/rankings/publish', { method: 'POST', body: raw });
  }
  if (env.SERVICE_PAUSED === 'true') return response(refusal('service_paused', 'Service is paused.', 3600, 503));
  // Benchmarks share the hosting allowance and provider meters. This identity
  // cannot be supplied by a public user and carries no user information.
  const check = await gate.fetch('https://budget/admit', { method: 'POST', body: JSON.stringify({ client: 'b'.repeat(64), kind: 'benchmark' }) });
  if (!check.ok) return check;
  if (path.endsWith('/chat')) {
    const permit = await gate.fetch('https://budget/capacity/benchmark', { method: 'POST', body: '{}' });
    if (!permit.ok) return permit;
  }
  const raw = request.method === 'POST' ? await boundedBody(request, 4_000_000) : undefined;
  if (raw === null) return response({ status: 413, body: { error: 'Conversation too large.' } });
  return (env.EVALUATOR || env.API).fetch(new Request('https://api.internal' + path, { method: request.method,
    headers: { 'Content-Type': 'application/json', 'X-Bailout-Evaluation': 'authorized' }, body: raw }));
}

async function boundedBody(request: Request, maximum: number): Promise<Blob | null> {
  if (Number(request.headers.get('Content-Length')) > maximum) return null;
  const chunks: Uint8Array[] = []; let length = 0;
  if (request.body) for await (const chunk of request.body) {
    length += chunk.byteLength; if (length > maximum) return null; chunks.push(chunk);
  }
  return new Blob(chunks);
}
