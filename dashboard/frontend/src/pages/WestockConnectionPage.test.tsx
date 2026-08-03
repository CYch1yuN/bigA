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

async function renderPage() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  (api.westockConnection as ReturnType<typeof vi.fn>).mockResolvedValue(STATUS);
  renderWithProviders(<App />, { route: '/connections/westock' });
  await screen.findByText('Westock 数据连接');
}

describe('Westock connection center', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows cache-export honestly and lists controlled capabilities', async () => {
    await renderPage();
    expect(screen.getByText('未连接')).toBeInTheDocument();      // MCP 连接
    expect(screen.getByText('缓存导出桥')).toBeInTheDocument();   // transport hint
    expect(screen.getByText('不可用')).toBeInTheDocument();       // 缓存可用（无缓存）
    expect(screen.getByText('data_quote')).toBeInTheDocument();
    expect(screen.getByText('data_profile')).toBeInTheDocument();
    expect(screen.getAllByText('无缓存')).toHaveLength(2);
    expect(screen.getByText('当前 Dashboard 未配置可用的 MCP 直连授权，使用缓存导出桥。')).toBeInTheDocument();
  });

  it('reports refresh limitation without pretending success', async () => {
    (api.westockRefresh as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, accepted: false, transport: 'cache_export', requested: [], message: '请由 WorkBuddy 导出缓存后刷新页面。' });
    await renderPage();
    fireEvent.click(screen.getByRole('button', { name: '刷新连接状态' }));
    expect(await screen.findByText('请由 WorkBuddy 导出缓存后刷新页面。')).toBeInTheDocument();
    await waitFor(() => expect(api.westockRefresh).toHaveBeenCalledWith([]));
  });
});
