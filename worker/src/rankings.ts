import type { Storage } from './types.js';

export const SUITE = 'bailout-setup-v1';
export interface Qualification {
  id: string; fingerprint: string; suite: string; trials: number; passed: number;
  last_trials?: number; last_passed?: number; last_critical_failures?: number;
  runs: number; critical_failures: number; native_tools: boolean; evaluated_at: string;
}
export interface Manifest {
  schema: 1; suite: string; generated_at: string; run_id: string; harness_sha: string;
  models: Qualification[];
}
interface HealthRow { [key: string]: SqlStorageValue; model: string; success_ewma: number; latency_ms: number; samples: number; updated: number }
const validId = (id: unknown): id is string => typeof id === 'string' && id.length <= 256 && /^[\w.-]+\/(?:[\w.-]+\/)*[\w.-]+:free$/.test(id);
const integer = (n: unknown): n is number => typeof n === 'number' && Number.isSafeInteger(n) && n >= 0 && n <= 100000;

export function validateManifest(value: unknown, now = Date.now()): Manifest {
  if (!value || typeof value !== 'object') throw new Error('Invalid manifest');
  const data = value as Manifest;
  if (data.schema !== 1 || data.suite !== SUITE || !/^[a-f0-9]{40}$/.test(data.harness_sha)
      || typeof data.run_id !== 'string' || !/^[\w.-]{1,100}$/.test(data.run_id)
      || !Number.isFinite(Date.parse(data.generated_at)) || Date.parse(data.generated_at) > now + 60000
      || Date.parse(data.generated_at) < now - 48 * 3600000 || !Array.isArray(data.models)
      || !data.models.length || data.models.length > 100) throw new Error('Invalid manifest');
  const ids = new Set<string>();
  for (const row of data.models) {
    if (!row || !validId(row.id) || ids.has(row.id) || !/^[a-f0-9]{64}$/.test(row.fingerprint)
        || row.suite !== SUITE || ![row.trials, row.passed, row.runs, row.critical_failures].every(integer)
        || row.trials < 1 || row.passed > row.trials || row.runs < 1 || row.runs > row.trials
        || row.critical_failures > row.trials || typeof row.native_tools !== 'boolean'
        || !Number.isFinite(Date.parse(row.evaluated_at)) || Date.parse(row.evaluated_at) > now + 60000
        || Date.parse(row.evaluated_at) < now - 30 * 86400000) throw new Error('Invalid qualification');
    if ([row.last_trials, row.last_passed, row.last_critical_failures].some(n => n !== undefined)
        && (!integer(row.last_trials) || row.last_trials < 1 || row.last_trials > 10 || row.last_trials > row.trials
        || !integer(row.last_passed) || row.last_passed > row.last_trials || row.last_passed > row.passed
        || !integer(row.last_critical_failures) || row.last_critical_failures > row.last_trials || row.last_critical_failures > row.critical_failures)) throw new Error('Invalid batch');
    ids.add(row.id);
  }
  // Store the schema only, never arbitrary additional fields or fixture logs.
  return { schema: 1, suite: SUITE, generated_at: data.generated_at, run_id: data.run_id,
    harness_sha: data.harness_sha, models: data.models.map(r => ({ id: r.id, fingerprint: r.fingerprint,
      suite: r.suite, trials: r.trials, passed: r.passed, runs: r.runs, critical_failures: r.critical_failures,
      native_tools: r.native_tools, evaluated_at: r.evaluated_at,
      last_trials: r.last_trials, last_passed: r.last_passed, last_critical_failures: r.last_critical_failures })) };
}

export class Rankings {
  constructor(private storage: Storage) {
    storage.sql.exec('CREATE TABLE IF NOT EXISTS model_rankings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)');
    storage.sql.exec('CREATE TABLE IF NOT EXISTS route_observations (model TEXT PRIMARY KEY, success_ewma REAL NOT NULL, latency_ms REAL NOT NULL, samples INTEGER NOT NULL, updated INTEGER NOT NULL)');
  }
  snapshot(now = Date.now()) {
    const row = [...this.storage.sql.exec<{ value: string }>('SELECT value FROM model_rankings WHERE id = 1')][0];
    const data: Manifest | null = row ? JSON.parse(row.value) : null;
    const health = Object.fromEntries([...this.storage.sql.exec<HealthRow>('SELECT * FROM route_observations WHERE updated > ?', now - 86400000)].map(r => [r.model, r]));
    return { ...data, models: data?.models ?? [], health,
      stale: !data || now - Date.parse(data.generated_at) > 48 * 3600000 };
  }
  publish(value: unknown, now = Date.now()) {
    const next = validateManifest(value, now);
    return this.storage.transactionSync(() => {
      const previous = this.snapshot(now);
      if (previous.run_id === next.run_id) return { ok: true, unchanged: true };
      if (previous.generated_at && Date.parse(next.generated_at) <= Date.parse(previous.generated_at)) throw new Error('Older ranking snapshot');
      // An incomplete evaluation run cannot erase untested models. Quality
      // evidence expires independently; pricing is always checked live.
      const merged = new Map(previous.models.filter(r => now - Date.parse(r.evaluated_at) <= 30 * 86400000).map(r => [r.id, r]));
      for (const row of next.models) {
        const existing = merged.get(row.id);
        if (existing && Date.parse(row.evaluated_at) < Date.parse(existing.evaluated_at)) throw new Error('Older model evidence');
        merged.set(row.id, row);
      }
      if (merged.size > 100) throw new Error('Too many models');
      const saved = { ...next, models: [...merged.values()] };
      if (previous.run_id) this.storage.sql.exec('INSERT INTO model_rankings VALUES (2, ?) ON CONFLICT(id) DO UPDATE SET value = excluded.value', JSON.stringify({ ...previous, health: undefined, stale: undefined }));
      this.storage.sql.exec('INSERT INTO model_rankings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET value = excluded.value', JSON.stringify(saved));
      return { ok: true, models: saved.models.length };
    });
  }
  record(model: string, outcome: string, latency: number, now = Date.now()) {
    if (!validId(model) || !['success', 'provider_timeout', 'provider_unavailable', 'upstream_authentication', 'upstream_quota', 'provider_rate_limited', 'upstream_rate_limited', 'response_too_long', 'request_failed', 'unexpected_cost'].includes(outcome)
        || !Number.isSafeInteger(latency) || latency < 0 || latency > 180000) return;
    const row = [...this.storage.sql.exec<HealthRow>('SELECT * FROM route_observations WHERE model = ?', model)][0];
    const success = outcome === 'success' ? 1 : 0;
    const prior = row && now - row.updated < 3600000 ? row.success_ewma : 1;
    this.storage.sql.exec('INSERT INTO route_observations VALUES (?, ?, ?, ?, ?) ON CONFLICT(model) DO UPDATE SET success_ewma = excluded.success_ewma, latency_ms = excluded.latency_ms, samples = excluded.samples, updated = excluded.updated',
      model, .8 * prior + .2 * success, row ? .8 * row.latency_ms + .2 * latency : latency, (row?.samples ?? 0) + 1, now);
    this.storage.sql.exec('DELETE FROM route_observations WHERE updated <= ?', now - 30 * 86400000);
  }
}
