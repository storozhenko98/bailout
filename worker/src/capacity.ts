import type { Storage } from "./types.js";
interface Attempt { [key: string]: SqlStorageValue; started: number; tokens: number; route: string; pending: number }
interface Quota { rpm: number; rpd: number; tpm: number; tpd: number }
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
  constructor(storage: Storage) {
    this.storage = storage;
    storage.sql.exec('CREATE TABLE IF NOT EXISTS benchmark_usage (day INTEGER PRIMARY KEY, requests INTEGER NOT NULL)');
    this.sql = storage.sql;
    this.sql.exec('CREATE TABLE IF NOT EXISTS inference_attempts (id TEXT PRIMARY KEY, provider TEXT NOT NULL, started INTEGER NOT NULL, tokens INTEGER NOT NULL, route TEXT NOT NULL, pending INTEGER NOT NULL)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS inference_window ON inference_attempts(provider, started)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS inference_health (route TEXT PRIMARY KEY, until INTEGER NOT NULL)');
  }
  benchmark(now = Date.now()) {
    return this.storage.transactionSync(() => {
      const day = Math.floor(now / 86400000);
      this.sql.exec('DELETE FROM benchmark_usage WHERE day < ?', day);
      const used = [...this.sql.exec<{ requests: number }>('SELECT requests FROM benchmark_usage WHERE day = ?', day)][0]?.requests ?? 0;
      if (used >= 1000) return { status: 429, body: { error: 'Daily evaluation request allowance reached.', code: 'benchmark_quota', retry_after_seconds: Math.ceil(((day + 1) * 86400000 - now) / 1000) } };
      this.sql.exec('INSERT INTO benchmark_usage VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET requests = requests + 1', day);
      return { status: 200, body: { ok: true } };
    });
  }
  reserve(provider: string, model: string, tokens = 0, now = Date.now(), quota?: Quota) {
    if (quota && (!['groq', 'mistral'].includes(provider) || ![quota.rpm, quota.rpd, quota.tpm, quota.tpd].every(n => Number.isSafeInteger(n) && n > 0 && n <= 10000000))) throw new Error('Invalid verified model quota');
    const limits = quota || PROVIDERS[provider];
    const scope = quota ? 'model' : 'provider';
    if (!limits || typeof model !== 'string' || model.length > 256 || !Number.isSafeInteger(tokens) || tokens < 0 || tokens > 8_000_000) throw new Error('Invalid provider reservation');
    return this.storage.transactionSync(() => {
      this.sql.exec('DELETE FROM inference_attempts WHERE started <= ?', now - 86400000);
      this.sql.exec('DELETE FROM inference_health WHERE until <= ?', now);
      const health = [...this.sql.exec<{ route: string; until: number }>('SELECT route, until FROM inference_health WHERE route IN (?, ?) ORDER BY until DESC', provider, provider + '/' + model)];
      const blocked = health[0]?.until;
      if (blocked > now) return { ok: false, code: 'provider_cooldown', scope: health.some(r => r.route === provider) ? 'provider' : 'model', retry_after_seconds: Math.ceil((blocked - now) / 1000) };
      // A context that cannot fit even an empty bucket needs another provider.
      if (limits.tpm && tokens > limits.tpm) return { ok: false, code: 'route_context_capacity', scope, retry_after_seconds: 0 };
      const rows = [...this.sql.exec<Attempt>('SELECT started, tokens, route, pending FROM inference_attempts WHERE provider = ? ORDER BY started', provider)];
      const running = rows.filter(r => r.pending && r.started > now - 120000);
      // ZAI's free Flash routes can allow only one in-flight request. Keep
      // their admission serial; other eligible models can still serve users.
      const modelConcurrency = provider === 'zai' ? 1 : 2;
      if (running.filter(r => r.route === model).length >= modelConcurrency) return { ok: false, code: 'route_busy', scope: 'model', retry_after_seconds: 2 };
      if (running.length >= 8) return { ok: false, code: 'provider_capacity', scope: 'provider', retry_after_seconds: 2 };
      let wait = 0;
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
      const permit = crypto.randomUUID();
      this.sql.exec('INSERT INTO inference_attempts VALUES (?, ?, ?, ?, ?, 1)', permit, provider, now, tokens, model);
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
