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
  requested?: string[];
  is_realtime?: boolean;
  request_id?: string;
  status?: string;
  target?: string;
  preset?: string;
  symbol?: string | null;
  capabilities?: string[];
  message: string;
}

export interface WestockRefreshJob {
  job_id: string;
  capability: string;
  scope: string;
  status: string;
  summary_only?: boolean;
  fetched_at?: string | null;
  cache_status?: string | null;
  data_as_of?: string | null;
  content_hash?: string | null;
  warning?: string | null;
}

export type WestockTarget =
  | { kind: 'stock'; symbols: string[]; preset?: string; capabilities?: string[]; allow_summary_only?: boolean; summary_only_symbols?: string[] }
  | { kind: 'market'; preset: string }
  | { kind: 'screener'; result_id: string; cache_scope: string; capability: string };

export interface WestockRefreshRequest {
  request_id: string;
  status: string;
  target: WestockTarget;
  jobs: WestockRefreshJob[];
  created_at?: string | null;
  claimed_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  expires_at?: string | null;
  attempts?: number;
  warnings?: string[];
  status_detail?: string | null;
}

export interface WestockCoverage {
  schema_version: number;
  capability_total: number;
  discovered_capabilities: string[];
  fresh_count: number;
  stale_count: number;
  unavailable_count: number;
  stock_matrix: Record<string, Record<string, string>>;
  stock_local_history: Record<string, boolean>;
  global_capabilities: Record<string, string>;
  query_scope_counts: Record<string, number>;
  latest_export_at?: string | null;
  local_history_available: boolean;
  warnings?: string[];
}

// ---- F5-A 运营只读类型 ----

export interface WestockOpsEnvelope<T> {
  schema_version: number;
  source: string;
  as_of: string;
  generated_at: string;
  availability: string;
  data: T;
  warnings: string[];
}

export interface WestockOpsCache {
  capability: string;
  scope: string;
  short_scope: string;
  scope_id: string | null;
  scope_type: string;
  group: string;
  file_state: string;
  in_expected_matrix: boolean;
  availability: string;
  cache_status: string;
  freshness_status: string | null;
  consumer_status: string;
  integrity: { valid: boolean; hash_verified: boolean; hash_status: string };
  age_seconds: number | null;
  ttl_seconds: number;
  expires_at: string | null;
  as_of: string | null;
  fetched_at: string | null;
  cached_at: string | null;
  last_refresh_status: string;
  failure_category: string | null;
  local_history_available: boolean;
  summary_only: boolean;
}

export interface WestockOpsCapability {
  capability: string;
  name: string;
  group: string;
  read_only: boolean;
  ttl_seconds: number;
  scope_count: number;
  usable: number;
  stale: number;
  unavailable: number;
  latest_ok_at: string | null;
  latest_fail_at: string | null;
  success_rate: number | null;
}

export interface WestockOpsSymbol {
  symbol: string;
  local_history_available: boolean;
  expected_count: number;
  usable: number;
  stale: number;
  unavailable: number;
}

export interface WestockOpsRequest {
  request_id: string;
  short_id: string;
  status: string;
  receipt_status: string;
  target: string | null;
  preset: string | null;
  symbols: string | string[] | null;
  job_counts: { ok: number; partial: number; failed: number; skipped: number; pending: number };
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  /** 脱敏：warning 只返回计数与固定分类；status_detail 只返回固定受控 code */
  warning_count: number;
  warning_categories: Record<string, number>;
  status_detail_code: string;
}

export interface WestockOpsSummaryData {
  physical_cache_count: number;
  expected_cell_count: number;
  total_cells: number;
  unexpected_physical_count: number;
  invalid_physical_count: number;
  availability: { available: number; unavailable: number };
  freshness: { fresh: number; stale: number; future_timestamp: number; invalid_timestamp: number; unavailable: number };
  consumer_status: { usable: number; unusable: number; not_validated: number };
  integrity: { hash_mismatch: number; hash_unverified: number; pending_evidence: number };
  usable_fresh_stale: number;
  valid_coverage: number | null;
  capabilities: WestockOpsCapability[];
  symbols: WestockOpsSymbol[];
  requests: {
    total: number;
    status_counts: Record<string, number>;
    receipt_status_counts: Record<string, number>;
    job_counts: { ok: number; partial: number; failed: number; skipped: number; pending: number };
    avg_duration_seconds: number | null;
    recent_20: WestockOpsRequest[];
  };
  failures: {
    job_failure_categories: Record<string, number>;
    request_failure_categories: Record<string, number>;
    failed_job_count: number;
    failed_request_count: number;
    receipt_audit_issues: Record<string, number>;
    receipt_audit_issue_count: number;
    orphan_receipt_count: number;
    invalid_receipt_file_count: number;
  };
  ttl_expiring: { within_5min: number; within_1h: number; expired: number };
  as_of_lag: { current_date: string | null; unknown_count: number; per_capability: Record<string, { as_of: string | null; lag_days: number | null }> };
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

// ---- Phase C：个股深度数据（Westock 缓存聚合，仅研究展示） ----

export type DeepCapabilityStatus = 'fresh' | 'stale' | 'unavailable';

export interface CapabilityMeta {
  status: DeepCapabilityStatus;
  as_of: string | null;
  fetched_at: string | null;
  cache_age_seconds: number | null;
}

export interface DeepBaseResponse {
  ok: boolean;
  schema_version: number;
  symbol: string;
  source: string;
  as_of: string | null;
  fetched_at: string | null;
  cache_status: DeepCapabilityStatus;
  is_realtime: boolean;
  transport: string;
  availability: Record<string, DeepCapabilityStatus>;
  capability_meta: Record<string, CapabilityMeta | null>;
  data: Record<string, unknown>;
  warnings: string[];
}

export interface StockFundamentalsResponse extends DeepBaseResponse { data: {
  profile: Record<string, unknown> | null;
  financials: { summary?: Record<string, unknown>; balance_sheet?: Record<string, unknown>; income_statement?: Record<string, unknown>; cash_flow?: Record<string, unknown> } | null;
  forecast: Record<string, unknown> | null;
} }
export interface StockOwnershipResponse extends DeepBaseResponse { data: {
  shareholders: Record<string, unknown> | null;
  dividend: Record<string, unknown> | null;
  buyback: Record<string, unknown> | null;
} }
export interface StockFundsResponse extends DeepBaseResponse { data: {
  margin: Record<string, unknown> | null;
  block_trade: Record<string, unknown>[] | null;
  fund_flow: Record<string, unknown> | null;
  northbound: Record<string, unknown> | null;
  lhb: Record<string, unknown>[] | null;
  chip_distribution: Record<string, unknown> | null;
} }
export interface StockIntelItem {
  category: string;
  title?: string;
  summary?: string;
  source?: string;
  date?: string;
  url?: string;
  org?: string;
  rating?: string;
  target_price?: number;
  ann_type?: string;
  /** reports/announcements：完整 datetime，不截断 */
  time?: string;
  /** announcements：更新时间 */
  update_time?: string;
  /** announcements：公告类型 */
  type?: string;
  /** reports：券商机构名 */
  institution?: string;
}
export interface StockIntelResponse extends DeepBaseResponse { data: {
  items: StockIntelItem[];
  total: number;
  [key: string]: unknown;
} }
export interface StockEventsResponse extends DeepBaseResponse { data: {
  events: { category?: string; date?: string; title?: string }[] | null;
  risk: Record<string, unknown> | null;
} }
export interface StockTechnicalResponse extends DeepBaseResponse { data: {
  indicators: Record<string, unknown> | null;
  note?: string;
} }

// ---- Phase D：市场研究中心类型 ----

export interface MarketOverviewResponse extends DeepBaseResponse { data: {
  overview: {
    score?: number; sentiment?: number; trend?: number; liquidity?: number;
    breadth?: number; volatility?: number; risk_level?: string; summary?: string;
    dimensions?: { trend?: number; sentiment?: number; liquidity?: number; breadth?: number; volatility?: number; risk?: number };
  } | null;
} }
export interface MarketDistributionResponse extends DeepBaseResponse { data: {
  distribution: {
    rise_count?: number; fall_count?: number; flat_count?: number;
    limit_up_count?: number; limit_down_count?: number; total_amount?: number;
    bins?: { label?: string; min_percent?: number; max_percent?: number; count?: number }[];
  } | null;
} }
export interface MarketHotResponse extends DeepBaseResponse { data: {
  hot: {
    stocks?: { rank?: number; symbol?: string; name?: string; price?: number; change_percent?: number; heat?: number; reason?: string; local_history_available?: boolean }[];
    sectors?: { rank?: number; code?: string; name?: string; change_percent?: number; heat?: number; leader_symbol?: string; leader_name?: string; leader_local_history_available?: boolean }[];
  } | null;
} }
export interface MarketSectorsResponse extends DeepBaseResponse { data: {
  sectors: { code?: string; name?: string; sector_type?: string; change_percent?: number; amount?: number; turnover_rate?: number; rise_count?: number; fall_count?: number; leader_symbol?: string; leader_name?: string; leader_local_history_available?: boolean }[] | null;
} }
export interface MarketIndexesResponse extends DeepBaseResponse { data: {
  indexes: { code?: string; name?: string; price?: number; change?: number; change_percent?: number; amount?: number; volume?: number }[] | null;
} }
export interface MarketConstituentsResponse extends DeepBaseResponse { data: {
  constituents: { symbol?: string; name?: string; weight?: number; industry?: string; local_history_available?: boolean }[];
  index_code: string;
} }
export interface MarketIndustryChainResponse extends DeepBaseResponse { data: {
  chains: { code?: string; name?: string; description?: string; upstream?: ChainNode[]; midstream?: ChainNode[]; downstream?: ChainNode[] }[] | null;
} }
export interface ChainNode { code?: string; name?: string; node_type?: string; related_symbols?: string[]; }
export interface MarketMacroResponse extends DeepBaseResponse { data: {
  indicators: { code?: string; name?: string; value?: number; unit?: string; period?: string; release_date?: string; previous?: number; forecast?: number; importance?: string }[] | null;
} }
export interface MarketCalendarResponse extends DeepBaseResponse { data: {
  items: { category?: string; date?: string; time?: string; title?: string; importance?: string; country?: string; actual?: number; forecast?: number; previous?: number; url?: string }[];
  total: number;
} }
export interface MarketFundsResponse extends DeepBaseResponse { data: {
  funds: { margin_balance?: number | null; margin_change?: number | null; northbound_net?: number | null; northbound_holding?: number | null; southbound_net?: number | null; date?: string | null };
} }
export interface MarketEventItem { category?: string; date?: string; title?: string; summary?: string; severity?: string; symbols?: string[]; url?: string; }
export interface MarketEventsResponse extends DeepBaseResponse { data: {
  events: MarketEventItem[];
  total: number;
} }

// ---- Phase E：选股中心类型 ----

export type ScreenerMode = 'condition' | 'strategy' | 'factor' | 'label';
export interface ScreenerUniverse { type: 'local' | 'index' | 'sector' | 'industry_chain'; value: string | null; }
export interface ScreenerCondition { field: string; operator: string; value: unknown; }
export interface ScreenerQuery {
  mode: ScreenerMode;
  universe: ScreenerUniverse;
  conditions: ScreenerCondition[];
  strategy: Record<string, unknown> | null;
  factor: Record<string, unknown> | null;
  labels: { values: string[]; match: string } | null;
  sort: { field: string; direction: string };
  limit: number;
}
export interface ScreenerRow {
  symbol?: string; name?: string; score?: number; rank?: number; price?: number;
  change_percent?: number; industry?: string; sector?: string; reason?: string;
  matched_conditions?: string[]; matched_labels?: string[]; factor_values?: Record<string, number>;
  local_history_available?: boolean;
}
export interface ScreenerRunResponse {
  ok: boolean; schema_version: number; result_id: string; mode: ScreenerMode;
  source: string; as_of: string | null; source_fetched_at: string | null; generated_at: string;
  cache_status: string; is_realtime: boolean; transport: string;
  availability: Record<string, string>; query: ScreenerQuery;
  data: { items: ScreenerRow[]; total: number }; warnings: string[];
  cache_scope: string;
}
export interface SavedFilter { id: string; name: string; query: ScreenerQuery; created_at: string; updated_at: string; }
export interface Candidate {
  symbol: string; name?: string; source_result_id: string; note?: string;
  added_at: string; local_history_available?: boolean;
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
  westockRefresh: (body: { target?: string; preset?: string; symbol?: string }) =>
    request<WestockRefreshResult>('/api/connections/westock/refresh', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  westockCoverage: (params: { capability?: string; scope?: string; status?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.capability) q.set('capability', params.capability);
    if (params.scope) q.set('scope', params.scope);
    if (params.status) q.set('status', params.status);
    const suffix = q.toString() ? `?${q.toString()}` : '';
    return request<WestockCoverage>(`/api/connections/westock/coverage${suffix}`);
  },
  westockRefreshRequests: (params: { status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set('status', params.status);
    if (params.limit != null) q.set('limit', String(params.limit));
    if (params.offset != null) q.set('offset', String(params.offset));
    const suffix = q.toString() ? `?${q.toString()}` : '';
    return request<{ ok: boolean; items: WestockRefreshRequest[]; total: number }>(
      `/api/connections/westock/refresh-requests${suffix}`);
  },
  westockCreateRefreshRequest: (body: {
    target: string;
    preset?: string;
    symbols?: string[];
    capabilities?: string[];
    allow_summary_only?: boolean;
    result_id?: string;
  }) =>
    request<WestockRefreshRequest & { ok: boolean }>(
      '/api/connections/westock/refresh-requests', {
        method: 'POST',
        body: JSON.stringify(body),
      }),
  westockCancelRefreshRequest: (requestId: string) =>
    request<WestockRefreshRequest & { ok: boolean }>(
      `/api/connections/westock/refresh-requests/${requestId}`, {
        method: 'DELETE',
      }),
  // ---- F5-A 运营只读端点 ----
  westockOpsQuery: <T>(path: string, params: Record<string, string | number | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v != null && v !== '') q.set(k, String(v));
    }
    const suffix = q.toString() ? `?${q.toString()}` : '';
    return request<WestockOpsEnvelope<T>>(`/api/connections/westock/operations/${path}${suffix}`);
  },
  westockOpsSummary: () =>
    request<WestockOpsEnvelope<WestockOpsSummaryData>>('/api/connections/westock/operations/summary'),
  westockOpsCaches: (params: {
    capability?: string; symbol?: string; scope_type?: string; freshness?: string;
    consumer_status?: string; failure_category?: string; limit?: number; offset?: number;
  } = {}) => api.westockOpsQuery<{
    total: number; coverage_total: number; inventory_total: number;
    unexpected_physical_count: number;
    limit: number; offset: number; items: WestockOpsCache[];
  }>('caches', params),
  westockOpsCapabilities: (params: { capability?: string; limit?: number; offset?: number } = {}) =>
    api.westockOpsQuery<{ total: number; limit: number; offset: number; items: WestockOpsCapability[] }>('capabilities', params),
  westockOpsSymbols: (params: { symbol?: string; limit?: number; offset?: number } = {}) =>
    api.westockOpsQuery<{ total: number; limit: number; offset: number; items: WestockOpsSymbol[] }>('symbols', params),
  westockOpsRequests: (params: {
    request_status?: string; limit?: number; offset?: number;
  } = {}) => api.westockOpsQuery<{ total: number; limit: number; offset: number; items: WestockOpsRequest[] }>('requests', params),
  westockOpsFailures: () =>
    api.westockOpsQuery<{
      job_failure_categories: Record<string, number>;
      request_failure_categories: Record<string, number>;
      failed_job_count: number;
      failed_request_count: number;
      receipt_audit_issues: Record<string, number>;
      receipt_audit_issue_count: number;
      orphan_receipt_count: number;
      invalid_receipt_file_count: number;
    }>('failures'),
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
  // Phase C 深度数据（只读 Westock 缓存聚合）
  stocksFundamentals: (symbol: string) => request<StockFundamentalsResponse>(`/api/stocks/${encodeURIComponent(symbol)}/fundamentals`),
  stocksOwnership: (symbol: string) => request<StockOwnershipResponse>(`/api/stocks/${encodeURIComponent(symbol)}/ownership`),
  stocksFunds: (symbol: string) => request<StockFundsResponse>(`/api/stocks/${encodeURIComponent(symbol)}/funds`),
  stocksIntel: (symbol: string, params: { category?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    q.set('limit', String(params.limit ?? 20));
    q.set('offset', String(params.offset ?? 0));
    if (params.category) q.set('category', params.category);
    return request<StockIntelResponse>(`/api/stocks/${encodeURIComponent(symbol)}/intel?${q.toString()}`);
  },
  stocksEvents: (symbol: string) => request<StockEventsResponse>(`/api/stocks/${encodeURIComponent(symbol)}/events`),
  stocksTechnical: (symbol: string) => request<StockTechnicalResponse>(`/api/stocks/${encodeURIComponent(symbol)}/technical`),
  // ---- Phase D：市场研究中心（只读 Westock 缓存） ----
  marketOverview: () => request<MarketOverviewResponse>('/api/market/overview'),
  marketDistribution: () => request<MarketDistributionResponse>('/api/market/distribution'),
  marketHot: () => request<MarketHotResponse>('/api/market/hot'),
  marketSectors: () => request<MarketSectorsResponse>('/api/market/sectors'),
  marketIndexes: () => request<MarketIndexesResponse>('/api/market/indexes'),
  marketConstituents: (indexCode: string) => request<MarketConstituentsResponse>(`/api/market/indexes/${encodeURIComponent(indexCode)}/constituents`),
  marketIndustryChain: () => request<MarketIndustryChainResponse>('/api/market/industry-chain'),
  marketMacro: () => request<MarketMacroResponse>('/api/market/macro'),
  marketCalendar: (params: { start_date?: string; end_date?: string; category?: string; importance?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.start_date) q.set('start_date', params.start_date);
    if (params.end_date) q.set('end_date', params.end_date);
    if (params.category) q.set('category', params.category);
    if (params.importance) q.set('importance', params.importance);
    q.set('limit', String(params.limit ?? 50));
    q.set('offset', String(params.offset ?? 0));
    return request<MarketCalendarResponse>(`/api/market/calendar?${q.toString()}`);
  },
  marketFunds: () => request<MarketFundsResponse>('/api/market/funds'),
  marketEvents: () => request<MarketEventsResponse>('/api/market/events'),
  // ---- Phase E：选股中心 ----
  screenerRun: (query: ScreenerQuery) => request<ScreenerRunResponse>('/api/screener/run', { method: 'POST', body: JSON.stringify(query) }),
  screenerResult: (resultId: string) => request<ScreenerRunResponse>(`/api/screener/results/${encodeURIComponent(resultId)}`),
  screenerSavedList: () => request<{ ok: boolean; items: SavedFilter[] }>('/api/screener/saved'),
  screenerSavedCreate: (payload: { name: string; query: ScreenerQuery }) => request<{ ok: boolean; saved_id: string; name: string }>('/api/screener/saved', { method: 'POST', body: JSON.stringify(payload) }),
  screenerSavedDelete: (savedId: string) => request<{ ok: boolean; deleted: string }>(`/api/screener/saved/${encodeURIComponent(savedId)}`, { method: 'DELETE' }),
  screenerCandidatesList: () => request<{ ok: boolean; items: Candidate[]; note: string }>('/api/screener/candidates'),
  screenerCandidatesAdd: (payload: { symbol: string; source_result_id: string; note?: string }) => request<{ ok: boolean; symbol: string; added: boolean }>('/api/screener/candidates', { method: 'POST', body: JSON.stringify(payload) }),
  screenerCandidatesDelete: (symbol: string) => request<{ ok: boolean; deleted: string }>(`/api/screener/candidates/${encodeURIComponent(symbol)}`, { method: 'DELETE' }),
};
