// Provider quotas are shared by every Worker isolate. Reserve each upstream
// attempt, including retries, before sending it. No prompts or client IDs here.
export const PROVIDERS = Object.freeze({
  openrouter: { rpm: 18, rpd: 1000, tpm: 0, tpd: 0 },
  groq: { rpm: 28, rpd: 950, tpm: 7800, tpd: 190000 },
});

export class CapacityLedger {
  constructor(storage) {
    this.storage = storage;
    this.sql = storage.sql;
    this.sql.exec('CREATE TABLE IF NOT EXISTS inference_attempts (id TEXT PRIMARY KEY, provider TEXT NOT NULL, started INTEGER NOT NULL, tokens INTEGER NOT NULL, route TEXT NOT NULL, pending INTEGER NOT NULL)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS inference_window ON inference_attempts(provider, started)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS inference_health (route TEXT PRIMARY KEY, until INTEGER NOT NULL)');
  }
  reserve(provider, model, tokens = 0, now = Date.now()) {
    const limits = PROVIDERS[provider];
    if (!limits || typeof model !== 'string' || model.length > 256 || !Number.isSafeInteger(tokens) || tokens < 0 || tokens > 1000000) throw new Error('Invalid provider reservation');
    return this.storage.transactionSync(() => {
      this.sql.exec('DELETE FROM inference_attempts WHERE started <= ?', now - 86400000);
      this.sql.exec('DELETE FROM inference_health WHERE until <= ?', now);
      const health = [...this.sql.exec('SELECT route, until FROM inference_health WHERE route IN (?, ?) ORDER BY until DESC', provider, provider + '/' + model)];
      const blocked = health[0]?.until;
      if (blocked > now) return { ok: false, code: 'provider_cooldown', scope: health.some(r => r.route === provider) ? 'provider' : 'model', retry_after_seconds: Math.ceil((blocked - now) / 1000) };
      // A context that cannot fit even an empty bucket needs another provider.
      if (limits.tpm && tokens > limits.tpm) return { ok: false, code: 'route_context_capacity', retry_after_seconds: 0 };
      const rows = [...this.sql.exec('SELECT started, tokens, route, pending FROM inference_attempts WHERE provider = ? ORDER BY started', provider)];
      const running = rows.filter(r => r.pending && r.started > now - 120000);
      if (running.filter(r => r.route === model).length >= 2) return { ok: false, code: 'route_busy', scope: 'model', retry_after_seconds: 2 };
      if (running.length >= 8) return { ok: false, code: 'provider_capacity', scope: 'provider', retry_after_seconds: 2 };
      let wait = 0;
      for (const [duration, requests, budget] of [[60000, limits.rpm, limits.tpm], [86400000, limits.rpd, limits.tpd]]) {
        const active = rows.filter(r => r.started > now - duration);
        let count = active.length, used = active.reduce((n, r) => n + r.tokens, 0);
        for (const row of active) {
          if (count < requests && (!budget || used + tokens <= budget)) break;
          wait = Math.max(wait, row.started + duration - now);
          count--; used -= row.tokens;
        }
      }
      if (wait > 0) return { ok: false, code: 'provider_capacity', retry_after_seconds: Math.max(1, Math.ceil(wait / 1000)) };
      const permit = crypto.randomUUID();
      this.sql.exec('INSERT INTO inference_attempts VALUES (?, ?, ?, ?, ?, 1)', permit, provider, now, tokens, model);
      return { ok: true, permit };
    });
  }
  settle(permit, tokens) {
    if (typeof permit !== 'string' || (tokens !== null && (!Number.isSafeInteger(tokens) || tokens < 0 || tokens > 1000000))) throw new Error('Invalid usage');
    // Requests stay counted even when a provider rejects them. Only successful,
    // reported usage can reconcile a conservative token reservation.
    this.sql.exec('UPDATE inference_attempts SET tokens = COALESCE(?, tokens), pending = 0 WHERE id = ?', tokens, permit);
  }
  cooldown(provider, model, seconds, now = Date.now()) {
    if (!PROVIDERS[provider] || (model !== null && (typeof model !== 'string' || model.length > 256)) || !Number.isFinite(seconds)) throw new Error('Invalid cooldown');
    const route = model === null ? provider : provider + '/' + model;
    this.sql.exec('INSERT INTO inference_health VALUES (?, ?) ON CONFLICT(route) DO UPDATE SET until = MAX(until, excluded.until)', route, now + Math.min(86400, Math.max(1, seconds)) * 1000);
  }
}
