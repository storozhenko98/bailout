import type { Storage, HttpResult } from "./types.js";
// Dollar reservations, not a claim to read Cloudflare's real-time invoice.
export const POLICY = Object.freeze({
  allowanceMicroUsd: 35_000_000,
  attemptMicroUsd: 10,
  forwardMicroUsd: 300, // Python CPU plus bounded provider reservations/retries.
  windowHours: 31 * 24,
  clientMinute: 60,
  clientHour: 600,
  clientDay: 2000,
  globalMinute: 240,
  chatMinute: 120, // Admissions only; each upstream attempt has its own shared gate.
});
export const DOCS = 'https://bailout.dev/docs/#service-limits';
export function refusal(code: string, error: string, seconds = 60, status = 429, now = Date.now()) {
  return { status, body: { error, code, retry_after_seconds: Math.max(1, Math.ceil(seconds)),
    resets_at: new Date(now + Math.max(1, Math.ceil(seconds)) * 1000).toISOString(), docs: DOCS } };
}
export function budgetRefusal(reset: number, now = Date.now()) {
  const when = new Date(reset).toISOString();
  return refusal('budget_exhausted', `Bailout's shared hosting allowance is exhausted. New model requests are paused to control costs. Capacity begins returning at ${when}. You have not been charged. See ${DOCS}`, (reset - now) / 1000, 503, now);
}

// One globally named SQLite Durable Object; synchronous transactions reserve
// capacity before a caller can forward anything to the private Python service.
export class BudgetLedger {
  chatLimit: number; storage: Storage; sql: SqlStorage; allowance: number;
  constructor(storage: Storage, allowance: number = POLICY.allowanceMicroUsd, chatLimit: number = POLICY.chatMinute) {
    if (!Number.isSafeInteger(allowance) || allowance < 0 || allowance > POLICY.allowanceMicroUsd) throw new Error('Invalid budget configuration');
    if (!Number.isSafeInteger(chatLimit) || chatLimit < 1 || chatLimit > POLICY.chatMinute) throw new Error('Invalid admission configuration');
    this.chatLimit = chatLimit;
    this.storage = storage;
    this.sql = storage.sql;
    this.allowance = allowance;
    this.sql.exec('CREATE TABLE IF NOT EXISTS budget (hour INTEGER PRIMARY KEY, reserved INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, value TEXT NOT NULL, expires INTEGER NOT NULL)');
    this.sql.exec('CREATE INDEX IF NOT EXISTS client_expiry ON clients(expires)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS global_state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)');
  }
  admit(client: string, kind: string, now = Date.now()): HttpResult {
    if (!/^[a-f0-9]{64}$/.test(client) || !['chat', 'read', 'status', 'benchmark'].includes(kind)) throw new Error('Invalid internal admission request');
    return this.storage.transactionSync(() => {
      const hour = Math.floor(now / 3_600_000), day = Math.floor(hour / 24), minute = Math.floor(now / 60_000);
      this.sql.exec('DELETE FROM budget WHERE hour < ?', hour - POLICY.windowHours);
      const totals = this.sql.exec<{ reserved: number; first: number | null }>('SELECT COALESCE(SUM(reserved), 0) AS reserved, MIN(hour) AS first FROM budget').one();
      const reset = ((totals.first ?? hour) + POLICY.windowHours + 1) * 3_600_000;
      const cost = POLICY.attemptMicroUsd + (kind === 'status' ? 0 : POLICY.forwardMicroUsd);
      if (totals.reserved + cost > this.allowance) return budgetRefusal(reset, now);
      // Account for gate traffic, including requests rejected by fair-use rules.
      this.sql.exec('INSERT INTO budget VALUES (?, ?) ON CONFLICT(hour) DO UPDATE SET reserved = reserved + excluded.reserved', hour, POLICY.attemptMicroUsd);
      const global = [...this.sql.exec<{ value: string }>('SELECT value FROM global_state WHERE id = 1')][0];
      const g: Record<string, number> = global ? JSON.parse(global.value) : {};
      if (g.minute !== minute) { g.minute = minute; g.requests = 0; g.chats = 0; }
      if (g.cleanupHour !== hour) {
        this.sql.exec('DELETE FROM clients WHERE expires <= ?', now);
        g.cleanupHour = hour;
      }
      const chat = kind === 'chat' || kind === 'benchmark';
      if (g.requests >= POLICY.globalMinute || (chat && g.chats >= this.chatLimit)) {
        return refusal('capacity_busy', 'Shared free capacity is busy. Try again after the indicated delay.', (minute + 1) * 60 - now / 1000, 429, now);
      }
      const row = [...this.sql.exec<{ value: string }>('SELECT value FROM clients WHERE id = ?', client)][0];
      const c: Record<string, number> = row ? JSON.parse(row.value) : {};
      for (const [window, stamp] of [['minute', minute], ['hour', hour], ['day', day]] as const) {
        if (c[window] !== stamp) { c[window] = stamp; c[window + 'Count'] = 0; }
      }
      // Only the authenticated operator route can use this kind. Its separate
      // daily evaluation meter bounds bootstrap reruns. Raising public limits
      // must not expand evaluation's 30/minute, 1000/hour and 1000/day allowance.
      const benchmark = kind === 'benchmark';
      for (const [window, limit, duration] of [['minute', benchmark ? 30 : POLICY.clientMinute, 60], ['hour', benchmark ? 1000 : POLICY.clientHour, 3600], ['day', benchmark ? 1000 : POLICY.clientDay, 86400]] as const) {
        if (c[window + 'Count'] >= limit) return refusal('client_rate_limited', `This IP address has reached Bailout's ${window === 'day' ? 'daily' : window === 'hour' ? 'hourly' : 'per-minute'} fair-use limit. Wait before retrying.`, (c[window] + 1) * duration - now / 1000, 429, now);
      }
      g.requests++; if (chat) g.chats++;
      c.minuteCount++; c.hourCount++; c.dayCount++;
      this.sql.exec('INSERT INTO global_state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET value = excluded.value', JSON.stringify(g));
      this.sql.exec('INSERT INTO clients VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET value = excluded.value, expires = excluded.expires', client, JSON.stringify(c), (day + 2) * 86400000);
      if (kind !== 'status') this.sql.exec('UPDATE budget SET reserved = reserved + ? WHERE hour = ?', POLICY.forwardMicroUsd, hour);
      return { status: 200, body: { ok: true, budget: { mode: 'conservative_reservation', window_days: 31,
        allowance_usd: this.allowance / 1e6, reserved_usd: (totals.reserved + cost) / 1e6,
        earliest_capacity_return: new Date(reset).toISOString(), invoice_cap: false }, limits: { ...POLICY, chatMinute: this.chatLimit }, docs: DOCS } };
    });
  }
}
