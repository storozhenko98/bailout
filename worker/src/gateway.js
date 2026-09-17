import { BudgetLedger, budgetRefusal, refusal } from './budget.js';
import { PublicStats, publicStatsResponse } from './stats.js';

export class BudgetGuard {
  constructor(ctx, env) {
    this.ledger = new BudgetLedger(ctx.storage, Number(env.BUDGET_ALLOWANCE_MICRO_USD));
    this.stats = new PublicStats(ctx.storage);
  }
  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === '/stats' && request.method === 'GET') return Response.json(await this.stats.snapshot());
    if (path === '/record-request' && request.method === 'POST') {
      this.stats.recordRequest();
      return new Response(null, { status: 204 });
    }
    if (path !== '/admit' || request.method !== 'POST') return new Response(null, { status: 404 });
    const { client, kind } = await request.json();
    const result = this.ledger.admit(client, kind);
    return response(result);
  }
}

function response({ status, body }) {
  const headers = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Access-Control-Allow-Origin': 'https://bailout.dev' };
  if (body.retry_after_seconds) headers['Retry-After'] = String(body.retry_after_seconds);
  return Response.json(body, { status, headers });
}

// IPv6 /64 grouping prevents trivially rotating addresses within one allocation.
export function clientNetwork(ip) {
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
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/v1/stats') {
      if (request.method !== 'GET') return response({ status: 405, body: { error: 'Method not allowed.', code: 'method_not_allowed' } });
      return publicStatsResponse(env);
    }
    if (request.method === 'GET' && (url.pathname === '/' || url.pathname === '/health')) {
      return response({ status: 200, body: { ok: true, service: 'bailout', version: '0.4.0', free_only: true,
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
        const body = await check.json();
        if (body.code === 'budget_exhausted') {
          budgetReset = Date.parse(body.resets_at);
          budgetClosedUntil = Math.min(Date.now() + 60_000, budgetReset);
        }
        return response({ status: check.status, body });
      }
      if (url.pathname === '/v1/status') return check;
      let body;
      if (isChat) {
        if (!(request.headers.get('Content-Type') || '').toLowerCase().startsWith('application/json')) return response({ status: 415, body: { error: 'Use application/json.', code: 'invalid_content_type' } });
        if (Number(request.headers.get('Content-Length')) > 512000) return response({ status: 413, body: { error: 'Conversation exceeds 512 KB. Use /new.', code: 'body_too_large' } });
        const reader = request.body?.getReader();
        if (!reader) return response({ status: 400, body: { error: 'Missing request body.', code: 'invalid_body' } });
        const chunks = []; let size = 0;
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > 512000) { await reader.cancel(); return response({ status: 413, body: { error: 'Conversation exceeds 512 KB. Use /new.', code: 'body_too_large' } }); }
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
