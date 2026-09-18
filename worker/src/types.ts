export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface HttpResult { status: number; body: Record<string, unknown> }
export interface Storage {
  sql: SqlStorage;
  transactionSync<T>(fn: () => T): T;
  getAlarm(): Promise<number | null>;
  setAlarm(when: number): Promise<void>;
}
export interface Env {
  BUDGET: DurableObjectNamespace;
  API: Fetcher;
  EVALUATOR?: Fetcher;
  ASSETS: Fetcher;
  EDGE_IP_LIMIT: RateLimit;
  EDGE_GLOBAL_LIMIT: RateLimit;
  SERVICE_PAUSED?: string;
  BUDGET_ALLOWANCE_MICRO_USD?: string;
  CHAT_ADMISSIONS_PER_MINUTE?: string;
  PROVIDER_POOL?: string;
  GITHUB_STATS_TOKEN?: string;
  BENCHMARK_TOKEN?: string;
  RANKING_PUBLISH_TOKEN?: string;
}
