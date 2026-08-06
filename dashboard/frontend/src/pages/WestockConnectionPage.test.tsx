import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { api, WestockConnectionStatus } from '../api/client';
import { renderWithProviders } from '../test/render';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: {
      health: vi.fn(), login: vi.fn(), logout: vi.fn(), session: vi.fn(), changePassword: vi.fn(),
      safety: vi.fn(), dashboardSnapshot: vi.fn(), prepareAction: vi.fn(), executeAction: vi.fn(),
      jobsList: vi.fn(), jobGet: vi.fn(), jobPrepare: vi.fn(), jobCreate: vi.fn(),
      westockConnection: vi.fn(), westockRefresh: vi.fn(),
      westockCoverage: vi.fn(), westockRefreshRequests: vi.fn(),
      westockCreateRefreshRequest: vi.fn(), westockCancelRefreshRequest: vi.fn(),
      westockOpsSummary: vi.fn(), westockOpsCaches: vi.fn(),
      westockOpsCapabilities: vi.fn(), westockOpsSymbols: vi.fn(),
      westockOpsRequests: vi.fn(), westockOpsFailures: vi.fn(),
      westockHealth: vi.fn(), westockAlerts: vi.fn(),
      westockRecommendations: vi.fn(), westockTrends: vi.fn(),
    },
  };
});

// mock echarts：断言 init/setOption/resize/dispose 生命周期真实被调用
const echartsInit = vi.hoisted(() => vi.fn(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() })));
vi.mock('echarts', () => ({ init: echartsInit }));

const STATUS: WestockConnectionStatus = {
  ok: true, schema_version: 1, source: 'westock-mcp', as_of: '2026-08-03T04:00:00Z',
  fetched_at: null, cache_status: 'unavailable', is_realtime: false, transport: 'cache_export',
  availability: { connected: false, direct_mcp: false, cache_export: true, cache_available: false, manual_refresh: false },
  data: {
    connected: false, cache_available: false, capability_count: 2, fresh_count: 0, stale_count: 0, unavailable_count: 2,
    rate_limit: { state: 'inactive' }, circuit_breaker: { state: 'inactive' },
    capabilities: [
      { name: 'quote', tool: 'data_quote', ttl_seconds: 60, group: '行情', read_only: true, status: 'unavailable', cache_age_seconds: null, last_success_at: null, last_error_at: null, response_ms: null, circuit_state: 'not_observed' },
      { name: 'profile', tool: 'data_profile', ttl_seconds: 86400, group: '基本面', read_only: true, status: 'unavailable', cache_age_seconds: null, last_success_at: null, last_error_at: null, response_ms: null, circuit_state: 'not_observed' },
    ],
  },
  warnings: ['当前 Dashboard 未配置可用的 MCP 直连授权，使用缓存导出桥。'],
};

const COVERAGE = {
  schema_version: 1, capability_total: 32, discovered_capabilities: ['quote', 'profile'],
  fresh_count: 1, stale_count: 0, unavailable_count: 1,
  stock_matrix: {
    '600519.SH': { quote: 'fresh', profile: 'unavailable' },
    '000001.SZ': { quote: 'unavailable', profile: 'unavailable' },
  },
  stock_local_history: { '600519.SH': true, '000001.SZ': true },
  global_capabilities: { lhb: 'stale' },
  query_scope_counts: {}, latest_export_at: '2026-08-05T00:00:00Z', local_history_available: true,
};

const REQUEST = {
  request_id: 'a'.repeat(32), status: 'pending',
  target: { kind: 'stock', symbols: ['600519.SH'], preset: 'basic', allow_summary_only: false, summary_only_symbols: [] },
  jobs: [
    { job_id: 'b'.repeat(32), capability: 'quote', scope: '600519.SH', status: 'pending' },
    { job_id: 'c'.repeat(32), capability: 'profile', scope: '600519.SH', status: 'pending' },
  ],
  created_at: '2026-08-05T00:00:00Z', claimed_at: null, started_at: null,
  finished_at: null, expires_at: '2026-08-06T00:00:00Z', attempts: 0,
  warnings: [], status_detail: null,
};

async function renderPage() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  (api.westockConnection as ReturnType<typeof vi.fn>).mockResolvedValue(STATUS);
  (api.westockCoverage as ReturnType<typeof vi.fn>).mockResolvedValue(COVERAGE);
  (api.westockRefreshRequests as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [REQUEST], total: 1 });
  const rendered = renderWithProviders(<App />, { route: '/connections/westock' });
  await screen.findByText('Westock 数据连接');
  await screen.findByText('MCP 连接'); // 连接查询完成后指标网格才出现
  return rendered;
}

describe('Westock connection center (F3R2)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows cache-export honestly without tool names', async () => {
    await renderPage();
    expect(screen.getByText('未连接')).toBeInTheDocument();
    expect(screen.getByText('缓存导出桥')).toBeInTheDocument();
    expect(screen.queryByText('data_quote')).toBeNull();
    expect(screen.queryByText('data_profile')).toBeNull();
  });

  it('multi-symbol stock form with full presets submits symbols array', async () => {
    (api.westockCreateRefreshRequest as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, ...REQUEST });
    await renderPage();
    const input = screen.getByPlaceholderText('600519.SH,000001.SZ');
    fireEvent.change(input, { target: { value: '600519.SH,000001.SZ' } });
    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    expect(await screen.findByText('刷新请求已创建，等待 WorkBuddy 会话处理。')).toBeInTheDocument();
    await waitFor(() => expect(api.westockCreateRefreshRequest).toHaveBeenCalledWith(
      { target: 'stock', preset: 'basic', symbols: ['600519.SH', '000001.SZ'], allow_summary_only: false }));
  });

  it('rejects invalid symbol format without submitting', async () => {
    await renderPage();
    const input = screen.getByPlaceholderText('600519.SH,000001.SZ');
    fireEvent.change(input, { target: { value: '600519' } });
    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    expect(await screen.findByText(/格式非法/)).toBeInTheDocument();
    expect(api.westockCreateRefreshRequest).not.toHaveBeenCalled();
  });

  it('market form submits preset only', async () => {
    (api.westockCreateRefreshRequest as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, ...REQUEST });
    await renderPage();
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: 'market' } });
    fireEvent.change(screen.getByLabelText('预设'), { target: { value: 'overview' } });
    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    await waitFor(() => expect(api.westockCreateRefreshRequest).toHaveBeenCalledWith(
      { target: 'market', preset: 'overview' }));
  });

  it('screener form requires 32-hex result id (no manual scope input)', async () => {
    await renderPage();
    fireEvent.change(screen.getByLabelText('目标'), { target: { value: 'screener' } });
    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    expect(await screen.findByText(/32 位小写 hex/)).toBeInTheDocument();
    expect(api.westockCreateRefreshRequest).not.toHaveBeenCalled();
    // 无 scope/path 自由输入
    expect(screen.queryByPlaceholderText(/scope|path|命令|tool/i)).toBeNull();
  });

  it('queue shows jobs progress and cancel; no trade buttons', async () => {
    (api.westockCancelRefreshRequest as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, ...REQUEST, status: 'cancelled' });
    await renderPage();
    expect(screen.getByText('等待处理')).toBeInTheDocument();
    expect(screen.getByText('0/2')).toBeInTheDocument(); // jobs 进度
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(await screen.findByText('请求已取消。')).toBeInTheDocument();
    await waitFor(() => expect(api.westockCancelRefreshRequest).toHaveBeenCalledWith(REQUEST.request_id));
    const bad = screen.queryAllByRole('button').filter((b) => /交易|信号|订单/.test(b.textContent ?? ''));
    expect(bad).toHaveLength(0);
  });

  it('coverage matrix shows per-symbol local history and gaps', async () => {
    await renderPage();
    expect(screen.getByText('缓存覆盖矩阵（能力缺口标红）')).toBeInTheDocument();
    expect(screen.getByText('000001.SZ')).toBeInTheDocument();
    expect(screen.getAllByText('无缓存').length).toBeGreaterThanOrEqual(2); // 缺口
    expect(screen.getByText(/龙虎榜\(已过期\)/)).toBeInTheDocument();
  });

  it('renders fine with no requests (empty state) at mobile viewport', async () => {
    (api.westockRefreshRequests as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [], total: 0 });
    window.innerWidth = 390;
    window.innerHeight = 844;
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    (api.westockConnection as ReturnType<typeof vi.fn>).mockResolvedValue(STATUS);
    (api.westockCoverage as ReturnType<typeof vi.fn>).mockResolvedValue(COVERAGE);
    const rendered = renderWithProviders(<App />, { route: '/connections/westock' });
    await screen.findByText('Westock 数据连接');
    await screen.findByText('MCP 连接');
    await waitFor(() => {
      expect(screen.getByText('暂无刷新请求。')).toBeInTheDocument();
    });
    expect(screen.getAllByText('创建刷新请求').length).toBeGreaterThanOrEqual(1);
    rendered.unmount();
    window.innerWidth = 1024;
  });
});

// ------------------------------------------------------------------ //
// F5-A 运营 Tab
// ------------------------------------------------------------------ //
const OPS_ENVELOPE = (data: unknown) => ({
  schema_version: 1, source: 'westock-mcp', as_of: '2026-08-06',
  generated_at: '2026-08-06T00:00:00Z', availability: 'ok', warnings: [], data,
});

const OPS_SUMMARY = OPS_ENVELOPE({
  physical_cache_count: 2, expected_cell_count: 12, total_cells: 12,
  unexpected_physical_count: 0, invalid_physical_count: 0,
  availability: { available: 2, unavailable: 10 },
  freshness: { fresh: 1, stale: 1, future_timestamp: 0, invalid_timestamp: 0, unavailable: 10 },
  consumer_status: { usable: 1, unusable: 1, not_validated: 10 },
  integrity: { hash_mismatch: 0, hash_unverified: 1, pending_evidence: 0 },
  usable_fresh_stale: 1, valid_coverage: 0.0833,
  capabilities: [{
    capability: 'quote', name: 'quote', group: '行情', read_only: true, ttl_seconds: 60,
    scope_count: 1, usable: 1, stale: 0, unavailable: 0,
    latest_ok_at: '2026-08-06T00:00:00Z', latest_fail_at: null, success_rate: 1,
  }],
  symbols: [{ symbol: '600519.SH', local_history_available: true, expected_count: 20, usable: 1, stale: 0, unavailable: 19 }],
  requests: {
    total: 1,
    status_counts: { completed: 1, partial: 0, failed: 0, cancelled: 0, expired: 0, pending: 0, processing: 0 },
    receipt_status_counts: { valid: 1, missing: 0, invalid: 0, mismatch: 0, not_applicable: 0 },
    job_counts: { ok: 2, partial: 0, failed: 0, skipped: 0, pending: 0 },
    avg_duration_seconds: 10, recent_20: [],
  },
  failures: { upstream_empty: 2, unknown: 0 },
  ttl_expiring: { within_5min: 0, within_1h: 0, expired: 1 },
  as_of_lag: { current_date: '2026-08-06', per_capability: { quote: { as_of: '2026-08-05', lag_days: 0 } } },
});

const OPS_CACHES = OPS_ENVELOPE({
  total: 4, coverage_total: 3, inventory_total: 2, unexpected_physical_count: 1,
  limit: 20, offset: 0, items: [
    {
      capability: 'quote', scope: '600519.SH', short_scope: '600519.SH',
      scope_id: null, scope_type: 'stock', group: '行情', file_state: 'valid', in_expected_matrix: true,
      availability: 'available', cache_status: 'fresh', freshness_status: 'fresh',
      consumer_status: 'usable', integrity: { valid: true, hash_verified: true, hash_status: 'verified' },
      age_seconds: 10, ttl_seconds: 60, expires_at: '2026-08-06T00:00:00Z', as_of: '2026-08-05',
      fetched_at: '2026-08-05T15:00:00Z', cached_at: '2026-08-05T15:00:00Z',
      last_refresh_status: 'ok', failure_category: null, local_history_available: true, summary_only: false,
    },
    {
      capability: 'technical', scope: '600519.SH', short_scope: '600519.SH',
      scope_id: null, scope_type: 'stock', group: '行情', file_state: 'valid', in_expected_matrix: true,
      availability: 'available', cache_status: 'stale', freshness_status: 'stale',
      consumer_status: 'unusable', integrity: { valid: true, hash_verified: false, hash_status: 'unverified' },
      age_seconds: 3600, ttl_seconds: 300, expires_at: null, as_of: null,
      fetched_at: '2026-08-05T01:00:00Z', cached_at: '2026-08-05T01:00:00Z',
      last_refresh_status: 'failed', failure_category: 'consumer_validation_failed',
      local_history_available: true, summary_only: false,
    },
    {
      capability: 'filter', scope: 'q_aaaaaaa…aaaa', short_scope: 'q_aaaaaaa…aaaa',
      scope_id: null, scope_type: 'query', group: '选股', file_state: 'missing', in_expected_matrix: true,
      availability: 'unavailable', cache_status: 'unavailable', freshness_status: 'unavailable',
      consumer_status: 'not_validated', integrity: { valid: false, hash_verified: false, hash_status: 'unverified' },
      age_seconds: null, ttl_seconds: 300, expires_at: null, as_of: null,
      fetched_at: null, cached_at: null, last_refresh_status: 'never', failure_category: null,
      local_history_available: false, summary_only: false,
    },
    {
      capability: 'quote', scope: 'invalid_scope', short_scope: '非法 Scope',
      scope_id: 'u1', scope_type: 'invalid', group: '行情', file_state: 'invalid_envelope', in_expected_matrix: false,
      availability: 'unavailable', cache_status: 'unavailable', freshness_status: 'unavailable',
      consumer_status: 'not_validated', integrity: { valid: false, hash_verified: false, hash_status: 'unverified' },
      age_seconds: null, ttl_seconds: null, expires_at: null, as_of: null,
      fetched_at: null, cached_at: null, last_refresh_status: 'never', failure_category: 'invalid_envelope',
      local_history_available: false, summary_only: false,
    },
  ],
});

const OPS_SYMBOLS = OPS_ENVELOPE({
  total: 2, limit: 20, offset: 0, items: [
    { symbol: '600519.SH', local_history_available: true, expected_count: 20, usable: 1, stale: 0, unavailable: 19 },
    { symbol: '601318.SH', local_history_available: false, expected_count: 18, usable: 1, stale: 0, unavailable: 17 },
  ],
});

const OPS_REQUESTS = OPS_ENVELOPE({
  total: 1, limit: 10, offset: 0, items: [{
    request_id: 'd'.repeat(32), short_id: 'd'.repeat(8), status: 'completed',
    receipt_status: 'valid', target: 'stock',
    preset: 'basic', symbols: ['600519.SH'],
    job_counts: { ok: 2, partial: 0, failed: 0, skipped: 0, pending: 0 },
    created_at: '2026-08-05T00:00:00Z', started_at: '2026-08-05T00:01:00Z',
    finished_at: '2026-08-05T00:02:00Z', duration_seconds: 60,
    warning_count: 1, warning_categories: { upstream_empty: 1 },
    status_detail_code: 'completed_all',
  }],
});

const OPS_FAILURES = OPS_ENVELOPE({
  job_failure_categories: { upstream_empty: 2, consumer_validation_failed: 1, unknown: 0 },
  request_failure_categories: { worker_timeout: 1, unknown: 0 },
  failed_job_count: 3,
  failed_request_count: 1,
  receipt_audit_issues: { missing: 1, invalid: 0, mismatch: 0 },
  receipt_audit_issue_count: 1,
  orphan_receipt_count: 1,
  invalid_receipt_file_count: 1,
});

const OPS_CAPABILITIES = OPS_ENVELOPE({
  total: 1, limit: 20, offset: 0, items: [{
    capability: 'quote', name: 'quote', group: '行情', read_only: true, ttl_seconds: 60,
    scope_count: 1, usable: 1, stale: 0, unavailable: 0,
    latest_ok_at: '2026-08-06T00:00:00Z', latest_fail_at: null, success_rate: 1,
  }],
});

async function mockOps() {
  (api.westockOpsSummary as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_SUMMARY);
  (api.westockOpsCaches as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_CACHES);
  (api.westockOpsCapabilities as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_CAPABILITIES);
  (api.westockOpsSymbols as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_SYMBOLS);
  (api.westockOpsRequests as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_REQUESTS);
  (api.westockOpsFailures as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_FAILURES);
}

describe('Westock 运营中心（F5-A）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('初始只请求连接状态相关 API，不请求运营 API', async () => {
    await mockOps();
    await renderPage();
    expect(api.westockConnection).toHaveBeenCalledTimes(1);
    expect(api.westockCoverage).toHaveBeenCalledTimes(1);
    expect(api.westockOpsSummary).not.toHaveBeenCalled();
    expect(api.westockOpsCaches).not.toHaveBeenCalled();
    expect(api.westockOpsSymbols).not.toHaveBeenCalled();
    expect(api.westockOpsRequests).not.toHaveBeenCalled();
    expect(api.westockOpsFailures).not.toHaveBeenCalled();
  });

  it('六个中文 Tab 标签存在', async () => {
    await mockOps();
    await renderPage();
    for (const label of ['连接状态', '缓存质量', '能力覆盖', '股票覆盖', '刷新历史', '失败分析']) {
      expect(screen.getByRole('tab', { name: label })).toBeInTheDocument();
    }
  });

  it('点击 Tab 首次懒加载对应端点', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '缓存质量' }));
    await waitFor(() => expect(api.westockOpsCaches).toHaveBeenCalledTimes(1));
    expect((await screen.findAllByText('缓存质量')).length).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByRole('tab', { name: '股票覆盖' }));
    await waitFor(() => expect(api.westockOpsSymbols).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('601318.SH')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '失败分析' }));
    await waitFor(() => expect(api.westockOpsFailures).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('上游无数据')).toBeInTheDocument();
  });

  it('返回旧 Tab 不重复请求（staleTime 缓存）', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '缓存质量' }));
    await waitFor(() => expect(api.westockOpsCaches).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('tab', { name: '连接状态' }));
    await screen.findByText('连接说明');
    fireEvent.click(screen.getByRole('tab', { name: '缓存质量' }));
    await screen.findAllByText('缓存质量');
    await waitFor(() => expect(api.westockOpsCaches).toHaveBeenCalledTimes(1));
  });

  it('缓存质量 Tab 分开显示 fresh/stale 与 usable/unusable', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '缓存质量' }));
    expect(await screen.findByText('新鲜')).toBeInTheDocument();
    expect(screen.getByText('已过期')).toBeInTheDocument();
    expect(screen.getAllByText('可用').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('不可用').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('缺失')).toBeInTheDocument(); // 缺失 cell 展示
    expect(screen.getByText('q_aaaaaaa…aaaa')).toBeInTheDocument(); // q scope 缩写
    expect(screen.queryByText('data_quote')).toBeNull();
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('C:');
    expect(body).not.toContain('a'.repeat(64));
  });

  it('能力覆盖 Tab 显示有效覆盖率与成功率', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '能力覆盖' }));
    expect(await screen.findByText('有效覆盖率')).toBeInTheDocument();
    expect(screen.getByText('物理缓存文件')).toBeInTheDocument();
    expect(screen.getByText('预期覆盖单元')).toBeInTheDocument();
    expect(screen.getByText('8%')).toBeInTheDocument(); // 0.0833 → 8%
    expect(screen.getByText('100%')).toBeInTheDocument(); // success_rate 1
  });

  it('部分 API 失败独立降级（失败分析错误不影响其他 Tab）', async () => {
    await mockOps();
    (api.westockOpsFailures as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '失败分析' }));
    expect(await screen.findByText(/失败分析读取失败/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '刷新历史' }));
    expect(await screen.findByText(/成功 2/)).toBeInTheDocument(); // 历史正常
  });

  it('刷新历史显示短 ID 与耗时，无完整 request_id', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '刷新历史' }));
    expect(await screen.findByText(/dddddddd/)).toBeInTheDocument();
    expect(screen.getByText('1 分 0 秒')).toBeInTheDocument();
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('d'.repeat(32));
  });

  it('刷新历史显示中文回执状态，不显示英文 receipt key', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '刷新历史' }));
    expect(await screen.findByText('回执有效')).toBeInTheDocument();
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('receipt_status');
    expect(body).not.toContain('"valid"');
  });

  it('刷新历史脱敏：只显示 warning 计数/分类，不显示原文与 status_detail', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '刷新历史' }));
    expect(await screen.findByText('1 条（上游无数据 1）')).toBeInTheDocument();
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('2 项能力全部完成'); // status_detail 原文不出现
    expect(body).not.toContain('status_detail');
    expect(body).not.toContain('warning_categories');
  });

  it('cancelled/expired 请求显示无需回执（不误报缺失）', async () => {
    await mockOps();
    const base = OPS_REQUESTS.data as {
      total: number; limit: number; offset: number; items: Array<Record<string, unknown>>;
    };
    (api.westockOpsRequests as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_ENVELOPE({
      total: 2, limit: 10, offset: 0, items: [
        { ...base.items[0], request_id: 'a'.repeat(32), short_id: 'a'.repeat(8), status: 'cancelled', receipt_status: 'not_applicable' },
        { ...base.items[0], request_id: 'b'.repeat(32), short_id: 'b'.repeat(8), status: 'expired', receipt_status: 'not_applicable' },
      ],
    }));
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '刷新历史' }));
    expect(await screen.findAllByText('无需回执')).toHaveLength(2);
    expect(screen.queryByText('回执缺失')).toBeNull();
    expect(screen.getByText('已取消')).toBeInTheDocument();
    expect(screen.getByText('已过期')).toBeInTheDocument();
  });

  it('缓存质量对意外非法 scope 显示非法 Scope 与序号，不显示文件名', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '缓存质量' }));
    expect(await screen.findByText(/非法 Scope/)).toBeInTheDocument();
    expect(screen.getByText(/（u1）/)).toBeInTheDocument();
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('secret_token_xxx');
  });

  it('失败分析显示孤立回执与非法回执文件计数', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '失败分析' }));
    expect(await screen.findByText('孤立回执')).toBeInTheDocument();
    expect(screen.getByText('非法回执文件')).toBeInTheDocument();
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('orphan_receipt_count');
    expect(body).not.toContain('invalid_receipt_file_count');
  });

  it('能力覆盖显示预期/物理/意外/无效四个指标', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '能力覆盖' }));
    expect(await screen.findByText('预期覆盖单元')).toBeInTheDocument();
    expect(screen.getByText('物理缓存文件')).toBeInTheDocument();
    expect(screen.getByText('意外文件')).toBeInTheDocument();
    expect(screen.getByText('无效物理文件')).toBeInTheDocument();
  });

  it('缓存质量显示 coverage/inventory 双计数与 q 缩写', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '缓存质量' }));
    expect(await screen.findByText(/预期单元 3/)).toBeInTheDocument();
    expect(screen.getByText(/物理文件 2/)).toBeInTheDocument();
    expect(screen.getByText('q_aaaaaaa…aaaa')).toBeInTheDocument();
  });

  it('失败分析显示回执审计异常（中文）', async () => {
    await mockOps();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '失败分析' }));
    expect(await screen.findByText('回执审计异常')).toBeInTheDocument();
    expect(screen.getByText('回执缺失')).toBeInTheDocument();
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('receipt_audit_issues');
  });

  it('运营 Tab 无自动重试/删除/交易按钮', async () => {
    await mockOps();
    await renderPage();
    for (const tab of ['缓存质量', '刷新历史', '失败分析']) {
      fireEvent.click(screen.getByRole('tab', { name: tab }));
      await screen.findAllByText(tab);
    }
    const bad = screen.queryAllByRole('button').filter((b) => /自动重试|删除|强制|交易|信号|订单/.test(b.textContent ?? ''));
    expect(bad).toHaveLength(0);
  });

  it('移动端 390px 渲染正常，无 NaN/undefined/[object Object]', async () => {
    await mockOps();
    window.innerWidth = 390;
    window.innerHeight = 844;
    const rendered = await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '缓存质量' }));
    await screen.findAllByText('缓存质量');
    fireEvent.click(screen.getByRole('tab', { name: '股票覆盖' }));
    await screen.findByText('601318.SH');
    const body = document.body.textContent ?? '';
    for (const bad of ['NaN', 'undefined', '[object Object]']) {
      expect(body).not.toContain(bad);
    }
    rendered.unmount();
    window.innerWidth = 1024;
  });

  it('深浅主题切换后页面正常渲染', async () => {
    await mockOps();
    document.documentElement.setAttribute('data-theme', 'dark');
    const rendered = await renderPage();
    expect(screen.getByRole('tab', { name: '能力覆盖' })).toBeInTheDocument();
    document.documentElement.setAttribute('data-theme', 'light');
    expect(screen.getByRole('tab', { name: '能力覆盖' })).toBeInTheDocument();
    rendered.unmount();
    document.documentElement.removeAttribute('data-theme');
  });
});

// ===================================================================== //
// F5-B 健康中心
// ===================================================================== //
const OPS_HEALTH = OPS_ENVELOPE({
  overall_status: 'degraded', observed: true, note: 'Westock 异常不影响本地 curated、回测与模拟账本。',
  dimensions: {
    integrity: { status: 'critical', explanation: '缓存文件、哈希证据与回执审计的完整性。', alert_categories: ['hash_mismatch'], alert_count: 1 },
    consumer: { status: 'healthy', explanation: '正式消费者校验。', alert_categories: [], alert_count: 0 },
    freshness: { status: 'attention', explanation: '缓存时间戳与 TTL 时效。', alert_categories: ['stale_cache'], alert_count: 2 },
    coverage: { status: 'healthy', explanation: '预期覆盖矩阵。', alert_categories: [], alert_count: 0 },
    refresh_workflow: { status: 'attention', explanation: '刷新请求/回执闭环。', alert_categories: ['partial_refresh'], alert_count: 1 },
  },
  alert_summary: { critical: 1, high: 0, medium: 3, low: 0 },
});

const OPS_ALERTS = OPS_ENVELOPE({
  total: 2, limit: 20, offset: 0, items: [
    {
      alert_id: 'hash_mismatch-abc12345', severity: 'critical', category: 'hash_mismatch',
      title: '缓存哈希不一致', message: '预期单元缓存数据与可信导出证据不一致。',
      capability: 'quote', symbol: '600519.SH', short_scope: null, affected_count: 1,
      first_observed_at: '2026-08-06T00:00:00Z', last_observed_at: '2026-08-06T00:00:00Z',
      evidence: { cell_count: 1 }, recommendation_code: 'refresh_hash_mismatch', is_actionable: true,
    },
    {
      alert_id: 'stale_cache-00000000', severity: 'medium', category: 'stale_cache',
      title: '缓存已过期', message: '该能力存在超过 TTL 的缓存单元。',
      capability: 'quote', symbol: null, short_scope: null, affected_count: 3,
      first_observed_at: null, last_observed_at: null,
      evidence: { count: 3 }, recommendation_code: 'refresh_stale_capability', is_actionable: true,
    },
  ],
});

const OPS_RECS = OPS_ENVELOPE({
  total: 2, limit: 20, offset: 0, items: [
    {
      recommendation_id: 'refresh_stale_capability-abc', code: 'refresh_stale_capability', priority: 'medium',
      title: '刷新过期能力', reason: '存在超过 TTL 的缓存单元。', affected_count: 3,
      target_kind: 'stock', preset: null, symbols: ['600519.SH'], capabilities: ['quote'],
      short_scope: null, can_prefill_refresh: true, requires_workbuddy: false,
      allow_summary_only: false, warnings: [],
    },
    {
      recommendation_id: 'inspect_receipt_chain-def', code: 'inspect_receipt_chain', priority: 'critical',
      title: '核查回执审计链', reason: '回执缺失需人工核查。', affected_count: 1,
      target_kind: null, preset: null, symbols: [], capabilities: [],
      short_scope: null, can_prefill_refresh: false, requires_workbuddy: false,
      allow_summary_only: false, warnings: [],
    },
  ],
});

const zeroTrendDay = (date: string, reqs = 0) => ({
  date,
  requests_total: reqs,
  status_counts: { completed: reqs, partial: 0, failed: 0, cancelled: 0, expired: 0 },
  job_counts: { ok: reqs * 2, partial: 0, failed: 0, skipped: 0 },
  worker_timeout_count: 0, receipt_issue_count: 0,
  success_rate: reqs ? 1 : null, average_duration_seconds: reqs ? 30 : null,
});
const OPS_TRENDS_7 = OPS_ENVELOPE({
  window_days: 7, start_date: '2026-07-31', end_date: '2026-08-06', timezone: 'Asia/Shanghai',
  daily: [
    zeroTrendDay('2026-07-31'), zeroTrendDay('2026-08-01'), zeroTrendDay('2026-08-02'),
    zeroTrendDay('2026-08-03'), zeroTrendDay('2026-08-04'), zeroTrendDay('2026-08-05', 2),
    zeroTrendDay('2026-08-06', 2),
  ],
});
const OPS_TRENDS_30 = OPS_ENVELOPE({
  window_days: 30, start_date: '2026-07-08', end_date: '2026-08-06', timezone: 'Asia/Shanghai',
  daily: [zeroTrendDay('2026-07-08'), zeroTrendDay('2026-07-09'), zeroTrendDay('2026-07-10', 1)],
});

async function mockOpsF5B() {
  (api.westockHealth as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_HEALTH);
  (api.westockAlerts as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_ALERTS);
  (api.westockRecommendations as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_RECS);
  (api.westockTrends as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_TRENDS_7);
}

describe('Westock 健康中心（F5-B）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    echartsInit.mockClear();
  });

  it('初始不请求 F5-B API；点击 Tab 首次请求，返回不重复请求', async () => {
    await mockOpsF5B();
    await renderPage();
    expect(api.westockHealth).not.toHaveBeenCalled();
    expect(api.westockAlerts).not.toHaveBeenCalled();
    expect(api.westockRecommendations).not.toHaveBeenCalled();
    expect(api.westockTrends).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('tab', { name: '健康状态' }));
    await waitFor(() => expect(api.westockHealth).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('tab', { name: '连接状态' }));
    await screen.findByText('连接说明');
    fireEvent.click(screen.getByRole('tab', { name: '健康状态' }));
    await screen.findAllByText('健康状态');
    await waitFor(() => expect(api.westockHealth).toHaveBeenCalledTimes(1)); // staleTime 不重复
  });

  it('十个中文 Tab 标签存在', async () => {
    await mockOpsF5B();
    await renderPage();
    for (const label of ['连接状态', '缓存质量', '能力覆盖', '股票覆盖', '刷新历史', '失败分析',
      '健康状态', '活动告警', '维护建议', '刷新趋势']) {
      expect(screen.getByRole('tab', { name: label })).toBeInTheDocument();
    }
  });

  it('健康状态中文显示总体与五维', async () => {
    await mockOpsF5B();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '健康状态' }));
    expect(await screen.findByText('总体状态')).toBeInTheDocument();
    expect(screen.getByText('降级')).toBeInTheDocument(); // overall degraded
    expect(screen.getByText('严重')).toBeInTheDocument(); // integrity critical + critical 告警
    expect(screen.getByText('完整性')).toBeInTheDocument();
    expect(screen.getByText('刷新闭环')).toBeInTheDocument();
    expect(screen.getAllByText(/不影响本地 curated/).length).toBeGreaterThanOrEqual(1);
  });

  it('活动告警中文 severity/category + 严重度过滤', async () => {
    await mockOpsF5B();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '活动告警' }));
    expect(await screen.findByText('缓存哈希不一致')).toBeInTheDocument();
    expect(screen.getByText('哈希不一致')).toBeInTheDocument(); // 中文 category
    expect(screen.getByText('缓存已过期')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('严重度'), { target: { value: 'critical' } });
    await waitFor(() => expect(api.westockAlerts).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'critical', limit: 20, offset: 0 })));
    const body = document.body.textContent ?? '';
    expect(body).not.toContain('NaN');
    expect(body).not.toContain('undefined');
  });

  it('维护建议：可预填有按钮且不 POST；不可刷新建议无按钮', async () => {
    await mockOpsF5B();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    expect(await screen.findByText('刷新过期能力')).toBeInTheDocument();
    expect(screen.getByText('核查回执审计链')).toBeInTheDocument();
    // 只有一个"填入刷新表单"按钮（可预填项）；不可刷新项显示"仅检查说明"
    expect(screen.getAllByRole('button', { name: '填入刷新表单' })).toHaveLength(1);
    expect(screen.getByText('仅检查说明')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '填入刷新表单' }));
    // 切到连接 Tab 并预填，但绝不自动 POST
    expect(await screen.findByText(/已按维护建议预填：指定能力/)).toBeInTheDocument();
    expect(api.westockCreateRefreshRequest).not.toHaveBeenCalled();
    const input = screen.getByPlaceholderText('600519.SH,000001.SZ') as HTMLInputElement;
    expect(input.value).toContain('600519.SH');
  });

  it('刷新趋势：7/30 天切换与 ECharts 生命周期', async () => {
    await mockOpsF5B();
    await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '刷新趋势' }));
    expect(await screen.findByText('刷新趋势（只读）')).toBeInTheDocument();
    expect(screen.getByText('近 7 天')).toBeInTheDocument();
    await waitFor(() => expect(api.westockTrends).toHaveBeenCalledWith(7));
    await waitFor(() => expect(echartsInit).toHaveBeenCalled()); // 有数据 → 初始化图表
    fireEvent.click(screen.getByRole('button', { name: '近 30 天' }));
    (api.westockTrends as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_TRENDS_30);
    await waitFor(() => expect(api.westockTrends).toHaveBeenCalledWith(30));
  });

  it('无英文 category、完整 q scope、原始 JSON', async () => {
    await mockOpsF5B();
    await renderPage();
    for (const tab of ['健康状态', '活动告警', '维护建议', '刷新趋势']) {
      fireEvent.click(screen.getByRole('tab', { name: tab }));
      await screen.findAllByText(tab);
    }
    const body = document.body.textContent ?? '';
    for (const bad of ['NaN', 'undefined', '[object Object]', 'q_' + 'a'.repeat(64)]) {
      expect(body).not.toContain(bad);
    }
    expect(body).not.toContain('"alert_id"');
    expect(body).not.toContain('"evidence"');
  });

  it('移动端 390px 渲染正常（长文案与告警边界说明不溢出）', async () => {
    await mockOpsF5B();
    window.innerWidth = 390;
    window.innerHeight = 844;
    const rendered = await renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '健康状态' }));
    await screen.findByText('总体状态');
    fireEvent.click(screen.getByRole('tab', { name: '活动告警' }));
    await screen.findByText('缓存哈希不一致');
    const body = document.body.textContent ?? '';
    for (const bad of ['NaN', 'undefined', '[object Object]']) {
      expect(body).not.toContain(bad);
    }
    rendered.unmount();
    window.innerWidth = 1024;
  });
});

// ===================================================================== //
// F5-B 第一轮审核整改：维护建议 → 真实预填刷新表单（项一 / 项十）
// ===================================================================== //
const mkRec = (over: Record<string, unknown> = {}) => ({
  recommendation_id: 'rec-stock-caps', code: 'refresh_stale_capability', priority: 'medium',
  title: '刷新过期能力', reason: '存在超过 TTL 的缓存单元。', affected_count: 2,
  target_kind: 'stock', preset: null, symbols: ['600519.SH'], capabilities: ['quote', 'fund_flow'],
  short_scope: null, can_prefill_refresh: true, requires_workbuddy: false,
  allow_summary_only: false, warnings: [], ...over,
});
const recEnvelope = (items: unknown[]) =>
  OPS_ENVELOPE({ total: items.length, limit: 20, offset: 0, items });

// 项二反例场景：worker 超时只允许出现一条 high 告警
const ALERTS_WORKER_TIMEOUT = OPS_ENVELOPE({
  total: 1, limit: 20, offset: 0, items: [{
    alert_id: 'recent_worker_timeout-9f8e7d6c', severity: 'high', category: 'recent_worker_timeout',
    title: 'Worker 处理超时', message: '存在最近 24 小时内因 worker 超时失败的刷新请求。',
    capability: null, symbol: null, short_scope: null, affected_count: 1,
    first_observed_at: '2026-08-06T00:00:00Z', last_observed_at: '2026-08-06T00:00:00Z',
    evidence: { request_count: 1 }, recommendation_code: 'retry_recent_failure', is_actionable: true,
  }],
});

async function renderWithRecs(items: unknown[], alerts: unknown = OPS_ALERTS) {
  (api.westockHealth as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_HEALTH);
  (api.westockAlerts as ReturnType<typeof vi.fn>).mockResolvedValue(alerts);
  (api.westockRecommendations as ReturnType<typeof vi.fn>).mockResolvedValue(recEnvelope(items));
  (api.westockTrends as ReturnType<typeof vi.fn>).mockResolvedValue(OPS_TRENDS_7);
  (api.westockCreateRefreshRequest as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, ...REQUEST });
  return renderPage();
}
const lastCreateBody = () => {
  const calls = (api.westockCreateRefreshRequest as ReturnType<typeof vi.fn>).mock.calls;
  return (calls.length ? calls[calls.length - 1] : undefined)?.[0] as Record<string, unknown>;
};

describe('Westock 维护建议真实预填（F5-B 第一轮整改）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    echartsInit.mockClear();
  });

  it('①②③④ 点个股能力建议→切连接 Tab→指定能力模式→建议能力被勾选→未点创建前不 POST', async () => {
    await renderWithRecs([mkRec()]);
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    fireEvent.click(await screen.findByRole('button', { name: '填入刷新表单' }));

    // ① 自动切回连接 Tab（刷新表单可见）
    expect(await screen.findByRole('button', { name: '创建刷新请求' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '连接状态' })).toHaveAttribute('aria-selected', 'true');

    // ② 个股刷新方式切到「指定能力」，且预设下拉不再渲染
    const mode = screen.getByLabelText('个股刷新方式') as HTMLSelectElement;
    expect(mode.value).toBe('capabilities');
    expect(screen.getByTestId('stock-capabilities')).toBeInTheDocument();
    expect(screen.queryByLabelText('预设')).toBeNull();

    // ③ 建议给出的能力被真实勾选，未建议的能力不勾选
    expect((screen.getByLabelText('能力 quote') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('能力 fund_flow') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('能力 news') as HTMLInputElement).checked).toBe(false);
    expect((screen.getByPlaceholderText('600519.SH,000001.SZ') as HTMLInputElement).value)
      .toBe('600519.SH');

    // ④ 用户未点「创建刷新请求」前，绝不发起任何写请求
    expect(api.westockCreateRefreshRequest).not.toHaveBeenCalled();
  });

  it('⑤ 点创建后请求体含 capabilities 且完全不含 preset', async () => {
    await renderWithRecs([mkRec()]);
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    fireEvent.click(await screen.findByRole('button', { name: '填入刷新表单' }));
    await screen.findByTestId('stock-capabilities');

    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    await waitFor(() => expect(api.westockCreateRefreshRequest).toHaveBeenCalledTimes(1));
    const body = lastCreateBody();
    expect(body).toEqual({
      target: 'stock',
      capabilities: ['quote', 'fund_flow'],
      symbols: ['600519.SH'],
      allow_summary_only: false,
    });
    expect('preset' in body).toBe(false);
  });

  it('⑥ 非本地股票建议预填 allow_summary_only=true 并随请求提交', async () => {
    await renderWithRecs([mkRec({
      recommendation_id: 'rec-nonlocal',
      symbols: ['600519.SH', '000001.SZ'],
      capabilities: ['news'],
      allow_summary_only: true,
    })]);
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    fireEvent.click(await screen.findByRole('button', { name: '填入刷新表单' }));
    await screen.findByTestId('stock-capabilities');

    const chk = screen.getByLabelText('允许非本地股票摘要（summary-only）') as HTMLInputElement;
    expect(chk.checked).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    await waitFor(() => expect(api.westockCreateRefreshRequest).toHaveBeenCalledTimes(1));
    expect(lastCreateBody()).toEqual({
      target: 'stock',
      capabilities: ['news'],
      symbols: ['600519.SH', '000001.SZ'],
      allow_summary_only: true,
    });
  });

  it('⑦ screener 建议无 result_id 时不给预填按钮，只提示原筛选重新导出', async () => {
    await renderWithRecs([mkRec({
      recommendation_id: 'rec-screener', code: 'rerun_screener_export', priority: 'medium',
      title: '重新导出筛选结果', target_kind: 'screener', preset: null,
      symbols: [], capabilities: [], short_scope: 'q_ab12…',
      can_prefill_refresh: false, requires_workbuddy: true,
    })]);
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    expect(await screen.findByText('重新导出筛选结果')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '填入刷新表单' })).toBeNull();
    expect(screen.getByText('通过原筛选结果重新导出')).toBeInTheDocument();
    expect(api.westockCreateRefreshRequest).not.toHaveBeenCalled();
    // 完整 q scope 绝不外泄
    expect(document.body.textContent ?? '').not.toContain('q_' + 'a'.repeat(64));
  });

  it('⑧ 回执 / 消费者类建议始终没有刷新按钮', async () => {
    await renderWithRecs([
      mkRec({
        recommendation_id: 'rec-receipt', code: 'inspect_receipt_chain', priority: 'critical',
        title: '核查回执审计链', target_kind: null, symbols: [], capabilities: [],
        can_prefill_refresh: false, requires_workbuddy: false,
      }),
      mkRec({
        recommendation_id: 'rec-consumer', code: 'inspect_consumer_schema', priority: 'high',
        title: '核查数据模式', target_kind: null, symbols: [], capabilities: [],
        can_prefill_refresh: false, requires_workbuddy: false,
      }),
    ]);
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    expect(await screen.findByText('核查回执审计链')).toBeInTheDocument();
    expect(screen.getByText('核查数据模式')).toBeInTheDocument();
    expect(screen.queryAllByRole('button', { name: '填入刷新表单' })).toHaveLength(0);
    expect(screen.getAllByText('仅检查说明')).toHaveLength(2);
  });

  it('⑨ worker 超时只出现一条对应 high 告警，不重复计为刷新失败', async () => {
    await renderWithRecs([mkRec()], ALERTS_WORKER_TIMEOUT);
    fireEvent.click(screen.getByRole('tab', { name: '活动告警' }));
    expect(await screen.findByText('Worker 处理超时')).toBeInTheDocument();
    expect(screen.getAllByText('Worker 超时')).toHaveLength(1); // 中文 category 仅一行
    // high 徽标仅一枚（排除严重度下拉里的 <option>）
    const highBadges = screen.getAllByText('高').filter((el) => el.classList.contains('badge'));
    expect(highBadges).toHaveLength(1);
    expect(screen.queryByText('刷新失败')).toBeNull();          // 不产生重复失败告警
  });

  it('⑩ 预填能力用户可改，提交体反映修改后的能力集合', async () => {
    await renderWithRecs([mkRec()]);
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    fireEvent.click(await screen.findByRole('button', { name: '填入刷新表单' }));
    await screen.findByTestId('stock-capabilities');

    fireEvent.click(screen.getByLabelText('能力 fund_flow')); // 取消建议能力
    fireEvent.click(screen.getByLabelText('能力 news'));      // 追加自选能力
    expect((screen.getByLabelText('能力 fund_flow') as HTMLInputElement).checked).toBe(false);
    expect((screen.getByLabelText('能力 news') as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    await waitFor(() => expect(api.westockCreateRefreshRequest).toHaveBeenCalledTimes(1));
    expect(lastCreateBody()).toEqual({
      target: 'stock',
      capabilities: ['quote', 'news'],
      symbols: ['600519.SH'],
      allow_summary_only: false,
    });
  });

  it('⑪ 能力全部取消后拒绝提交，且不会退回 preset 模式偷发预设', async () => {
    await renderWithRecs([mkRec({ capabilities: ['quote'] })]);
    fireEvent.click(screen.getByRole('tab', { name: '维护建议' }));
    fireEvent.click(await screen.findByRole('button', { name: '填入刷新表单' }));
    await screen.findByTestId('stock-capabilities');

    fireEvent.click(screen.getByLabelText('能力 quote'));
    fireEvent.click(screen.getByRole('button', { name: '创建刷新请求' }));
    expect(await screen.findByText(/请选择 1–20 个能力/)).toBeInTheDocument();
    expect(api.westockCreateRefreshRequest).not.toHaveBeenCalled();
  });
});
