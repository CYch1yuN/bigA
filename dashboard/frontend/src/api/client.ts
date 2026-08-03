/** API 客户端：封装认证、CSRF、统一错误结构。 */

export interface ApiError {
  code: string;
  message: string;
}

export class ApiClientError extends Error {
  code: string;
  status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

export interface SafetyInfo {
  live_trading: boolean;
  broker_connected: boolean;
  allowed_actions: string[];
  forbidden_actions: string[];
  security_statement: string;
}

export interface SessionInfo {
  authenticated: boolean;
  username: string;
  expires_at: number;
}

export interface ActionResult {
  action: string;
  ok: boolean;
  stdout: string;
  stderr: string;
  exit_code: number;
  duration_ms: number;
  mock: boolean;
}

export interface HealthInfo {
  ok: boolean;
  status: string;
  service: string;
  version: string;
}

export interface DashboardSnapshot {
  ok: boolean;
  schema_version: number;
  generated_at: string;
  data_timestamp: string | null;
  artifact_date: string | null;
  mode: 'research_only';
  live_trading: false;
  broker_connected: false;
  availability: Record<string, boolean>;
  operations?: {
    available: boolean;
    verify: boolean;
    daily: boolean;
    weekly: boolean;
    rerun: boolean;
    backfill: boolean;
    note?: string;
  };
  latest_run: any | null;
  artifact_run: any | null;
  gate4b: any | null;
  accounts: any | null;
  signals: any | null;
  orders: any | null;
  quality: any | null;
  run_history: any[];
  disclaimer: string;
}

export type JobType = 'verify' | 'daily' | 'weekly' | 'rerun' | 'backfill';
export type JobState = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'interrupted' | 'skipped';

export interface JobParams {
  date?: string;
  task?: string;
  start_date?: string;
  end_date?: string;
}

export interface JobDailyResult {
  date: string;
  state: string;
  exit_code?: number | null;
  error?: string | null;
}

export interface JobSummary {
  exit_code?: number;
  duration_ms?: number;
  timed_out?: boolean;
  cli_state?: string;
  skipped?: string;
  start_date?: string;
  end_date?: string;
  trading_days?: number;
  succeeded?: number;
  failed?: number;
  skipped_days?: number;
}

export interface JobRecord {
  job_id: string;
  job_type: JobType;
  state: JobState;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  params: JobParams;
  daily_results: JobDailyResult[];
  summary: JobSummary;
  log: string[];
  error: string | null;
}

export interface JobListResponse {
  ok: boolean;
  jobs: JobRecord[];
}

export interface JobResponse {
  ok: boolean;
  job: JobRecord;
}

export type WestockCapabilityStatus = 'fresh' | 'stale' | 'unavailable' | 'unsupported';

export interface WestockCapability {
  name: string;
  tool: string;
  ttl_seconds: number;
  group: string;
  read_only: boolean;
  status: WestockCapabilityStatus;
  cache_age_seconds: number | null;
  last_success_at: string | null;
  last_error_at: string | null;
  response_ms: number | null;
  circuit_state: string;
}

export interface WestockConnectionStatus {
  ok: boolean;
  schema_version: number;
  source: 'westock-mcp';
  as_of: string;
  fetched_at: string | null;
  cache_status: WestockCapabilityStatus;
  is_realtime: boolean;
  transport: 'direct_mcp' | 'cache_export';
  availability: {
    connected: boolean;
    direct_mcp: boolean;
    cache_export: boolean;
    cache_available: boolean;
    manual_refresh: boolean;
  };
  data: {
    connected: boolean;
    cache_available: boolean;
    capability_count: number;
    fresh_count: number;
    stale_count: number;
    unavailable_count: number;
    capabilities: WestockCapability[];
    rate_limit: { state: string; reason?: string };
    circuit_breaker: { state: string; reason?: string };
  };
  warnings: string[];
}

export interface WestockRefreshResult {
  ok: boolean;
  accepted: boolean;
  transport: string;
  requested: string[];
  message: string;
}

// ---- Phase B 类型 ----

export type StockRange = '1m' | '3m' | '6m' | '1y' | '3y' | 'all';

export interface StockListItem {
  symbol: string;
  latest_trade_date: string;
  bar_count: number;
}

export interface StocksListResponse {
  ok: boolean;
  schema_version: number;
  source: string;
  as_of: string;
  fetched_at: string | null;
  cache_status: string;
  is_realtime: boolean;
  transport: string;
  availability: { curated: boolean; westock: boolean };
  data: { total: number; offset: number; limit: number; items: StockListItem[] };
  warnings: string[];
}

export interface StockBar {
  date: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: number | null;
  amount: number | null;
}

export interface StockHistoryResponse {
  ok: boolean;
  schema_version: number;
  symbol: string;
  source: string;
  as_of: string;
  fetched_at: string | null;
  cache_status: string;
  is_realtime: boolean;
  transport: string;
  availability: { curated: boolean; qfq: boolean };
  adjustment: 'raw' | 'qfq';
  range: string;
  data: { rows: StockBar[] };
  warnings: string[];
  message: string;
}

export interface StockSnapshotResponse {
  ok: boolean;
  schema_version: number;
  symbol: string;
  source: string;
  as_of: string;
  fetched_at: string | null;
  cache_status: string;
  is_realtime: boolean;
  transport: string;
  availability: { curated: boolean; westock_quote: boolean };
  data: {
    local: {
      date: string; close: string; open: string; high: string; low: string;
      volume: number | null; amount: number | null;
      change: string | null; change_percent: string | null;
    } | null;
    westock_quote: {
      price: number;
      change_percent: number | null;
      time: string | null;
      as_of: string | null;
      fetched_at: string | null;
      status: 'fresh' | 'stale';
    } | null;
  };
  warnings: string[];
}

export interface StockMinuteResponse {
  ok: boolean;
  schema_version: number;
  symbol: string;
  source: string;
  as_of: string | null;
  fetched_at: string | null;
  cache_status: string;
  is_realtime: boolean;
  transport: string;
  availability: { westock_minute: boolean };
  data: { rows: { time: string; price: number; volume: number | null }[] } | null;
  warnings: string[];
}

export interface StockSignalItem {
  signal_date: string | null;
  symbol: string | null;
  side: string;
  quantity: number | null;
  reason: string;
}

export interface StockOrderItem {
  signal_date: string | null;
  fill_date: string | null;
  symbol: string | null;
  side: string;
  quantity: number | null;
  status: string;
  fill_price: string | null;
  reason: string;
}

export interface StockPositionItem {
  account_id: string;
  symbol: string | null;
  total_quantity: number;
  sellable_quantity: number;
  avg_raw_cost: string;
}

export interface StockResearchResponse {
  ok: boolean;
  schema_version: number;
  symbol: string;
  source: string;
  as_of: string | null;
  fetched_at: string | null;
  cache_status: string;
  is_realtime: boolean;
  transport: string;
  availability: { artifacts: boolean };
  data: {
    as_of: string | null;
    signals: StockSignalItem[];
    orders: StockOrderItem[];
    positions: StockPositionItem[];
  };
  warnings: string[];
}

export interface JobPrepareResponse {
  ok: boolean;
  job_type: JobType;
  confirm_token: string;
  expires_in: number;
}

const CSRF_COOKIE = 'ashare_dash_csrf';

export function getCsrfToken(): string {
  const match = document.cookie.match(new RegExp(`(?:^|; )${CSRF_COOKIE}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : '';
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> | undefined),
  };
  if (options.method && options.method.toUpperCase() !== 'GET') {
    headers['X-CSRF-Token'] = getCsrfToken();
    headers['Content-Type'] = 'application/json';
  }
  let resp: Response;
  try {
    resp = await fetch(path, { ...options, headers });
  } catch {
    throw new ApiClientError('network_error', '无法连接服务器', 0);
  }
  let body: any = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  if (!resp.ok) {
    const code = body?.error?.code ?? 'unknown';
    const message = body?.error?.message ?? `请求失败（HTTP ${resp.status}）`;
    throw new ApiClientError(code, message, resp.status);
  }
  return body as T;
}

export const api = {
  health: () => request<HealthInfo>('/api/health'),
  login: (username: string, password: string) =>
    request<{ ok: boolean; username: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () =>
    request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  session: () => request<SessionInfo>('/api/auth/session'),
  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean; message: string }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  safety: () => request<SafetyInfo>('/api/safety'),
  dashboardSnapshot: () => request<DashboardSnapshot>('/api/dashboard/snapshot'),
  prepareAction: (action: string) =>
    request<{ ok: boolean; action: string; confirm_token: string; expires_in: number }>(
      '/api/actions/prepare',
      { method: 'POST', body: JSON.stringify({ action }) },
    ),
  executeAction: (action: string, confirmToken: string) =>
    request<{ ok: boolean; result: ActionResult }>('/api/actions/execute', {
      method: 'POST',
      body: JSON.stringify({ action, confirm_token: confirmToken }),
    }),
  // ---- 作业（Job）API ----
  jobsList: () => request<JobListResponse>('/api/jobs'),
  jobGet: (jobId: string) => request<JobResponse>(`/api/jobs/${jobId}`),
  jobPrepare: (jobType: JobType, params: JobParams = {}) =>
    request<JobPrepareResponse>('/api/jobs/prepare', {
      method: 'POST',
      body: JSON.stringify({ job_type: jobType, ...params }),
    }),
  jobCreate: (jobType: JobType, params: JobParams, confirmToken: string) =>
    request<JobResponse>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ job_type: jobType, ...params, confirm_token: confirmToken }),
    }),
  westockConnection: () => request<WestockConnectionStatus>('/api/connections/westock'),
  westockRefresh: (capabilities: string[] = []) =>
    request<WestockRefreshResult>('/api/connections/westock/refresh', {
      method: 'POST',
      body: JSON.stringify({ capabilities }),
    }),
  // ---- Phase B：个股行情与策略联动（只读） ----
  stocksList: (params: { query?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.query) q.set('query', params.query);
    q.set('limit', String(params.limit ?? 50));
    q.set('offset', String(params.offset ?? 0));
    return request<StocksListResponse>(`/api/stocks?${q.toString()}`);
  },
  stocksHistory: (symbol: string, params: { adjustment?: 'raw' | 'qfq'; range?: StockRange; end?: string } = {}) => {
    const q = new URLSearchParams();
    q.set('adjustment', params.adjustment ?? 'qfq');
    q.set('range', params.range ?? 'all');
    if (params.end) q.set('end', params.end);
    return request<StockHistoryResponse>(`/api/stocks/${encodeURIComponent(symbol)}/history?${q.toString()}`);
  },
  stocksSnapshot: (symbol: string) => request<StockSnapshotResponse>(`/api/stocks/${encodeURIComponent(symbol)}/snapshot`),
  stocksMinute: (symbol: string) => request<StockMinuteResponse>(`/api/stocks/${encodeURIComponent(symbol)}/minute`),
  stocksResearch: (symbol: string) => request<StockResearchResponse>(`/api/stocks/${encodeURIComponent(symbol)}/research`),
};
