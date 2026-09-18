import type { Storage } from "./types.js";
import { EVALUATION } from "./budget.js";
interface Attempt { [key: string]: SqlStorageValue; started: number; tokens: number; route: string; pending: number }
interface Quota { rpm: number; rpd: number; tpm: number; tpd: number }
export interface QueueRoute { provider: string; model: string; tokens: number; quota?: Quota }
interface Waiter { [key: string]: SqlStorageValue; seq: number; id: string; routes: string; created: number; touched: number }
export const WAITING = Object.freeze({ maximum: 64, routes: 128, leaseMs: 30000, lifetimeMs: 120000 });
// Provider quotas are shared by every Worker isolate. Reserve each upstream
// attempt, including retries, before sending it. No prompts or client IDs here.
export const PROVIDERS: Readonly<Record<string, { rpm: number; rpd: number; tpm: number; tpd: number }>> = Object.freeze({
  openrouter: { rpm: 18, rpd: 1000, tpm: 0, tpd: 0 },
  groq: { rpm: 28, rpd: 950, tpm: 7800, tpd: 190000 },
  mistral: { rpm: 1, rpd: 900, tpm: 48000, tpd: 450000 },
  zai: { rpm: 10, rpd: 900, tpm: 0, tpd: 0 },
  vercel: { rpm: 10, rpd: 900, tpm: 0, tpd: 0 },
});

export class CapacityLedger {
  storage: Storage; sql: SqlStorage;
  private attempts = new Map<string, Attempt[]>();
  private health = new Map<string, { route: string; until: number }[]>();
  constructor(storage: Storage) {
    this.storage = storage;
    storage.sql.exec('CREATE TABLE IF NOT EXISTS benchmark_usage (day INTEGER PRIMARY KEY, requests INTEGER NOT NULL)');
    this.sql = storage.sql;
    this.sql.exec('CREATE TABLE IF NOT EXISTS inference_attempts (id TEXT PRIMARY KEY, provider TEXT NOT NULL, started INTEGER NOT NULL, tokens INTEGER NOT NULL, route TEXT NOT NULL, pending INTEGER NOT NULL)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS inference_window ON inference_attempts(provider, started)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS inference_health (route TEXT PRIMARY KEY, until INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS capacity_migrations (id TEXT PRIMARY KEY)');
    // Only random per-response tickets and route/token requirements. Never
    // prompts, IP addresses, installation IDs, or a persistent session identity.
    this.sql.exec('CREATE TABLE IF NOT EXISTS inference_waiters (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, routes TEXT NOT NULL, created INTEGER NOT NULL, touched INTEGER NOT NULL)');
    this.storage.transactionSync(() => {
      // Repair the known 2026-09-18 17:01 UTC Vercel throttle: upgrade copy
      // containing "credits" was mistaken for account exhaustion. Live probes
      // and the recorded 429 confirmed a temporary per-model rate limit. This
      // one-time data correction never resets usage or future cooldowns.
      const id = '2026-09-18-vercel-credit-copy';
      if ([...this.sql.exec('SELECT id FROM capacity_migrations WHERE id = ?', id)].length) return;
      this.sql.exec('DELETE FROM inference_health WHERE route = ? AND until >= ? AND until < ?', 'vercel', Date.UTC(2026, 8, 18, 18, 1), Date.UTC(2026, 8, 18, 18, 2));
      this.sql.exec('INSERT INTO capacity_migrations VALUES (?)', id);
    });
  }
  benchmark(now = Date.now()) {
    return this.storage.transactionSync(() => {
      const day = Math.floor(now / 86400000);
      this.sql.exec('DELETE FROM benchmark_usage WHERE day < ?', day);
      const used = [...this.sql.exec<{ requests: number }>('SELECT requests FROM benchmark_usage WHERE day = ?', day)][0]?.requests ?? 0;
      if (used >= EVALUATION.day) return { status: 429, body: { error: 'Daily evaluation request allowance reached.', code: 'benchmark_quota', retry_after_seconds: Math.ceil(((day + 1) * 86400000 - now) / 1000) } };
      this.sql.exec('INSERT INTO benchmark_usage VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET requests = requests + 1', day);
      return { status: 200, body: { ok: true } };
    });
  }
  private validate(provider: string, model: string, tokens: number, quota?: Quota) {
    if (quota && (!['groq', 'mistral'].includes(provider) || ![quota.rpm, quota.rpd, quota.tpm, quota.tpd].every(n => Number.isSafeInteger(n) && n > 0 && n <= 10000000))) throw new Error('Invalid verified model quota');
    if (!PROVIDERS[provider] || typeof model !== 'string' || model.length > 256 || !Number.isSafeInteger(tokens) || tokens < 0 || tokens > 8_000_000) throw new Error('Invalid provider reservation');
  }
  private clean(now: number) {
    // One snapshot per synchronous admission transaction, even if many older
    // waiters are checked. Never reread a provider's day of usage per waiter.
    this.attempts.clear(); this.health.clear();
    this.sql.exec('DELETE FROM inference_attempts WHERE started <= ?', now - 86400000);
    this.sql.exec('DELETE FROM inference_health WHERE until <= ?', now);
    this.sql.exec('DELETE FROM inference_waiters WHERE touched <= ? OR created <= ?', now - WAITING.leaseMs, now - WAITING.lifetimeMs);
  }
  join(id: string, routes: QueueRoute[], now = Date.now()) {
    if (typeof id !== 'string' || !/^[a-f0-9-]{36}$/.test(id) || !Array.isArray(routes) || !routes.length || routes.length > WAITING.routes) throw new Error('Invalid waiting request');
    for (const route of routes) this.validate(route.provider, route.model, route.tokens, route.quota);
    routes = routes.map(({ provider, model, tokens, quota }) => ({ provider, model, tokens,
      ...(quota ? { quota: { rpm: quota.rpm, rpd: quota.rpd, tpm: quota.tpm, tpd: quota.tpd } } : {}) }));
    return this.storage.transactionSync(() => {
      this.clean(now);
      const existing = [...this.sql.exec<Waiter>('SELECT * FROM inference_waiters WHERE id = ?', id)][0];
      if (existing) {
        this.sql.exec('UPDATE inference_waiters SET routes = ?, touched = ? WHERE id = ?', JSON.stringify(routes), now, id);
      } else {
        const count = [...this.sql.exec<{ n: number }>('SELECT COUNT(*) AS n FROM inference_waiters')][0].n;
        if (count >= WAITING.maximum) return { ok: false, code: 'queue_full', retry_after_seconds: 15 };
        this.sql.exec('INSERT INTO inference_waiters (id, routes, created, touched) VALUES (?, ?, ?, ?)', id, JSON.stringify(routes), now, now);
      }
      return { ok: true };
    });
  }
  leave(id: string) {
    this.sql.exec('DELETE FROM inference_waiters WHERE id = ?', id);
  }
  private waiter(id: string, now: number) {
    const row = [...this.sql.exec<Waiter>('SELECT * FROM inference_waiters WHERE id = ?', id)][0];
    if (row) this.sql.exec('UPDATE inference_waiters SET touched = ? WHERE id = ?', now, id);
    return row;
  }
  private availability(provider: string, model: string, tokens: number, now: number, quota?: Quota) {
    const limits = quota || PROVIDERS[provider];
    const scope = quota ? 'model' : 'provider';
    const key = provider + '/' + model;
    let health = this.health.get(key);
    if (!health) {
      health = [...this.sql.exec<{ route: string; until: number }>('SELECT route, until FROM inference_health WHERE route IN (?, ?) ORDER BY until DESC', provider, key)];
      this.health.set(key, health);
    }
    const blocked = health[0]?.until;
    if (blocked > now) return { ok: false, code: 'provider_cooldown', scope: health.some(r => r.route === provider) ? 'provider' : 'model', retry_after_seconds: Math.ceil((blocked - now) / 1000) };
    // A context that cannot fit even an empty bucket needs another provider.
    if (limits.tpm && tokens > limits.tpm) return { ok: false, code: 'route_context_capacity', scope, retry_after_seconds: 0 };
    let rows = this.attempts.get(provider);
    if (!rows) {
      rows = [...this.sql.exec<Attempt>('SELECT started, tokens, route, pending FROM inference_attempts WHERE provider = ? ORDER BY started', provider)];
      this.attempts.set(provider, rows);
    }
    const running = rows.filter(r => r.pending && r.started > now - 120000);
    // ZAI's free Flash routes can allow only one in-flight request. Keep
    // their admission serial; other eligible models can still serve users.
    const modelConcurrency = provider === 'zai' ? 1 : 2;
    if (running.filter(r => r.route === model).length >= modelConcurrency) return { ok: false, code: 'route_busy', scope: 'model', retry_after_seconds: 2 };
    if (running.length >= 8) return { ok: false, code: 'provider_capacity', scope: 'provider', retry_after_seconds: 2 };
    let wait = 0;
    // Vercel documents per-model free-tier throttling. Live qualification
    // hit a temporary 429 on the sixth burst request; keep headroom while
    // retaining the provider-wide daily/minute gates below.
    const vercelMinute = rows.filter(r => r.route === model && r.started > now - 60000);
    const modelWait = provider === 'vercel' && vercelMinute.length >= 4 ? vercelMinute[vercelMinute.length - 4].started + 60000 - now : 0;
    for (const [duration, requests, budget] of [[60000, limits.rpm, limits.tpm], [86400000, limits.rpd, limits.tpd]]) {
      const active = rows.filter(r => r.started > now - duration && (!quota || r.route === model));
      let count = active.length, used = active.reduce((n, r) => n + r.tokens, 0);
      for (const row of active) {
        if (count < requests && (!budget || used + tokens <= budget)) break;
        wait = Math.max(wait, row.started + duration - now);
        count--; used -= row.tokens;
      }
    }
    if (wait > 0) return { ok: false, code: 'provider_capacity', scope, retry_after_seconds: Math.max(1, Math.ceil(wait / 1000)) };
    if (modelWait > 0) return { ok: false, code: 'provider_capacity', scope: 'model', retry_after_seconds: Math.max(1, Math.ceil(modelWait / 1000)) };
    return { ok: true };
  }
  private olderReady(provider: string, seq: number, now: number) {
    const older = [...this.sql.exec<Waiter>('SELECT * FROM inference_waiters WHERE seq < ? ORDER BY seq', seq)];
    return older.some(row => (JSON.parse(row.routes) as QueueRoute[]).some(route =>
      route.provider === provider && this.availability(route.provider, route.model, route.tokens, now, route.quota).ok));
  }
  peek(id: string, now = Date.now()) {
    return this.storage.transactionSync(() => {
      this.clean(now);
      const waiter = this.waiter(id, now);
      if (!waiter) return { ok: false, code: 'queue_expired', retry_after_seconds: 2 };
      let retry = 120;
      for (const route of JSON.parse(waiter.routes) as QueueRoute[]) {
        const available = this.availability(route.provider, route.model, route.tokens, now, route.quota);
        if (available.ok && !this.olderReady(route.provider, waiter.seq, now)) return { ok: true };
        retry = Math.min(retry, available.ok ? 2 : available.retry_after_seconds || 120);
      }
      return { ok: false, code: 'queue_wait', retry_after_seconds: retry };
    });
  }
  reserve(provider: string, model: string, tokens = 0, now = Date.now(), quota?: Quota, ticket?: string) {
    this.validate(provider, model, tokens, quota);
    return this.storage.transactionSync(() => {
      this.clean(now);
      const waiter = ticket ? this.waiter(ticket, now) : undefined;
      if (ticket && !waiter) return { ok: false, code: 'queue_expired', scope: 'model', retry_after_seconds: 2 };
      const available = this.availability(provider, model, tokens, now, quota);
      if (!available.ok) return available;
      // Unticketed evaluation/old-worker requests also yield to live waiters.
      // An unavailable older route cannot block an independent free pool.
      if (this.olderReady(provider, waiter?.seq ?? Number.MAX_SAFE_INTEGER, now)) return { ok: false, code: 'queue_wait', scope: 'provider', retry_after_seconds: 2 };
      const permit = crypto.randomUUID();
      this.sql.exec('INSERT INTO inference_attempts VALUES (?, ?, ?, ?, ?, 1)', permit, provider, now, tokens, model);
      // One upstream attempt spends a turn. Retries/follow-ups join the back.
      if (ticket) this.leave(ticket);
      return { ok: true, permit };
    });
  }
  settle(permit: string, tokens: number | null) {
    if (typeof permit !== 'string' || (tokens !== null && (!Number.isSafeInteger(tokens) || tokens < 0 || tokens > 8_000_000))) throw new Error('Invalid usage');
    // Requests stay counted even when a provider rejects them. Only successful,
    // reported usage can reconcile a conservative token reservation.
    this.sql.exec('UPDATE inference_attempts SET tokens = COALESCE(?, tokens), pending = 0 WHERE id = ?', tokens, permit);
  }
  cooldown(provider: string, model: string | null, seconds: number, now = Date.now()) {
    if (!PROVIDERS[provider] || (model !== null && (typeof model !== 'string' || model.length > 256)) || !Number.isFinite(seconds)) throw new Error('Invalid cooldown');
    const route = model === null ? provider : provider + '/' + model;
    this.sql.exec('INSERT INTO inference_health VALUES (?, ?) ON CONFLICT(route) DO UPDATE SET until = MAX(until, excluded.until)', route, now + Math.min(86400, Math.max(1, seconds)) * 1000);
  }
}
