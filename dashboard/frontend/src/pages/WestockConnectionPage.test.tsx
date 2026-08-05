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
    expect(screen.getByText(/lhb\(已过期\)/)).toBeInTheDocument();
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
    await waitFor(() => {
      expect(screen.getByText('暂无刷新请求。')).toBeInTheDocument();
    });
    expect(screen.getAllByText('创建刷新请求').length).toBeGreaterThanOrEqual(1);
    rendered.unmount();
    window.innerWidth = 1024;
  });
});
