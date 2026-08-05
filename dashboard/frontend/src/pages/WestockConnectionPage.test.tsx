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
    },
  };
});

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
