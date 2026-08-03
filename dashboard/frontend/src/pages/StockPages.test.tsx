import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { api } from '../api/client';
import { renderWithProviders } from '../test/render';

// jsdom 无 canvas：mock ECharts 组件，聚焦页面行为
vi.mock('../components/EChartsKLine', () => ({
  EChartsKLine: () => <div data-testid="mock-kline" />,
  mapTradeMarkers: () => [],
}));

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: {
      health: vi.fn(), login: vi.fn(), logout: vi.fn(), session: vi.fn(), changePassword: vi.fn(),
      safety: vi.fn(), dashboardSnapshot: vi.fn(), prepareAction: vi.fn(), executeAction: vi.fn(),
      jobsList: vi.fn(), jobGet: vi.fn(), jobPrepare: vi.fn(), jobCreate: vi.fn(),
      westockConnection: vi.fn(), westockRefresh: vi.fn(),
      stocksList: vi.fn(), stocksHistory: vi.fn(), stocksSnapshot: vi.fn(),
      stocksMinute: vi.fn(), stocksResearch: vi.fn(),
    },
  };
});

const HISTORY_OK = {
  ok: true, schema_version: 1, symbol: '600519.SH', source: 'local-curated', as_of: '2026-08-03',
  fetched_at: null, cache_status: 'available', is_realtime: false, transport: 'local-curated',
  availability: { curated: true, qfq: true }, adjustment: 'qfq', range: 'all',
  data: { rows: [
    { date: '2026-07-30', open: '1323.00', high: '1362.00', low: '1322.00', close: '1361.76', volume: 7187261, amount: 9.712135e9 },
    { date: '2026-07-31', open: '1330.03', high: '1355.72', low: '1325.77', close: '1350.60', volume: 5512752, amount: 7.373463e9 },
  ] },
  warnings: [], message: 'ok',
};

const HISTORY_EMPTY = {
  ...HISTORY_OK, cache_status: 'unavailable',
  availability: { curated: false, qfq: false }, data: { rows: [] },
  message: '本地无该标的 curated 历史行情',
};

const SNAPSHOT_OK = {
  ok: true, schema_version: 1, symbol: '600519.SH', source: 'local-curated+westock-cache', as_of: '2026-08-03',
  fetched_at: null, cache_status: 'available', is_realtime: false, transport: 'local-curated+westock-cache',
  availability: { curated: true, westock_quote: false },
  data: {
    local: { date: '2026-07-31', close: '1350.60', open: '1330.03', high: '1355.72', low: '1325.77',
             volume: 5512752, amount: 7.373463e9, change: '-11.16', change_percent: '-0.82' },
    westock_quote: null,
  },
  warnings: ['无 Westock quote 缓存'],
};

const MINUTE_MISSING = {
  ok: true, schema_version: 1, symbol: '600519.SH', source: 'westock-cache', as_of: '2026-08-03',
  fetched_at: null, cache_status: 'unavailable', is_realtime: false, transport: 'westock-cache',
  availability: { westock_minute: false }, data: null, warnings: ['Westock 分时缓存不存在'],
};

const RESEARCH_OK = {
  ok: true, schema_version: 1, symbol: '600519.SH', source: 'biga-artifacts', as_of: '2026-07-31',
  fetched_at: null, cache_status: 'available', is_realtime: false, transport: 'biga-artifacts',
  availability: { artifacts: true },
  data: {
    as_of: '2026-07-31',
    signals: [{ signal_date: '2026-07-31', symbol: '600519.SH', side: 'BUY', quantity: 100, reason: '测试' }],
    orders: [{ signal_date: '2026-07-31', fill_date: '2026-07-31', symbol: '600519.SH', side: 'BUY', quantity: 100, status: 'FILLED', fill_price: '1350.00', reason: '测试' }],
    positions: [{ account_id: 'paper-steady', symbol: '600519.SH', total_quantity: 100, sellable_quantity: 100, avg_raw_cost: '1330.00' }],
  },
  warnings: [],
};

async function renderDetail() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  (api.stocksHistory as ReturnType<typeof vi.fn>).mockResolvedValue(HISTORY_OK);
  (api.stocksSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue(SNAPSHOT_OK);
  (api.stocksMinute as ReturnType<typeof vi.fn>).mockResolvedValue(MINUTE_MISSING);
  (api.stocksResearch as ReturnType<typeof vi.fn>).mockResolvedValue(RESEARCH_OK);
  renderWithProviders(<App />, { route: '/stocks/600519.SH' });
  await screen.findByText('600519.SH');
}

describe('股票列表页', () => {
  beforeEach(() => vi.clearAllMocks());

  it('展示本地标的与搜索过滤', async () => {
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    (api.stocksList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, schema_version: 1, source: 'local-curated', as_of: '', fetched_at: null,
      cache_status: 'unavailable', is_realtime: false, transport: 'local-curated',
      availability: { curated: true, westock: false },
      data: { total: 1, offset: 0, limit: 100, items: [{ symbol: '600519.SH', latest_trade_date: '2026-07-31', bar_count: 268 }] },
      warnings: [],
    });
    renderWithProviders(<App />, { route: '/stocks' });
    expect(await screen.findByText('600519.SH')).toBeInTheDocument();
    expect(screen.getByText('2026-07-31')).toBeInTheDocument();
    expect(screen.getByText('本地数据可用')).toBeInTheDocument();
    expect(api.stocksList).toHaveBeenCalledWith({ query: undefined, limit: 100, offset: 0 });
  });

  it('空列表显示提示且不展示虚构行情', async () => {
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    (api.stocksList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, schema_version: 1, source: 'local-curated', as_of: '', fetched_at: null,
      cache_status: 'unavailable', is_realtime: false, transport: 'local-curated',
      availability: { curated: true, westock: false },
      data: { total: 0, offset: 0, limit: 100, items: [] }, warnings: [],
    });
    renderWithProviders(<App />, { route: '/stocks' });
    expect(await screen.findByText(/没有匹配的标的/)).toBeInTheDocument();
  });
});

describe('股票详情页', () => {
  beforeEach(() => vi.clearAllMocks());

  it('显示快照、K 线控制、区间、买卖点与信号订单持仓', async () => {
    await renderDetail();
    expect(await screen.findByText('1350.60')).toBeInTheDocument();   // 本地最新收盘
    expect(await screen.findByText(/-0.82%/)).toBeInTheDocument();    // 涨跌幅
    expect(await screen.findByRole('button', { name: '前复权' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '不复权' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '1月' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '3年' })).toBeInTheDocument();
    expect(screen.getByTestId('mock-kline')).toBeInTheDocument();
    // 信号/订单/持仓
    expect((await screen.findAllByText(/BUY 100 股/)).length).toBeGreaterThan(0);
    expect(screen.getByText(/paper-steady：100 股/)).toBeInTheDocument();
    // 分时缓存缺失提示
    expect(screen.getByText(/分时缓存不存在、已过期或数据无法标准化/)).toBeInTheDocument();
    // 买卖点说明（mock mapTradeMarkers 返回 []）
    expect(screen.getByText(/无匹配交易日的买卖点可叠加/)).toBeInTheDocument();
  });

  it('qfq/raw 与区间切换会重新请求 history', async () => {
    await renderDetail();
    await screen.findByRole('button', { name: '不复权' });
    fireEvent.click(screen.getByRole('button', { name: '不复权' }));
    await waitFor(() => expect(api.stocksHistory).toHaveBeenCalledWith(
      '600519.SH', { adjustment: 'raw', range: 'all' },
    ));
    await screen.findByRole('button', { name: '1月' }); // refetch 后控件恢复
    fireEvent.click(screen.getByRole('button', { name: '1月' }));
    await waitFor(() => expect(api.stocksHistory).toHaveBeenCalledWith(
      '600519.SH', { adjustment: 'raw', range: '1m' },
    ));
  });

  it('非本地股票显示尚未补跑提示，不显示虚构 K 线', async () => {
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    (api.stocksHistory as ReturnType<typeof vi.fn>).mockResolvedValue(HISTORY_EMPTY);
    (api.stocksSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ...SNAPSHOT_OK, availability: { curated: false, westock_quote: false }, data: { local: null, westock_quote: null }, warnings: [] });
    (api.stocksMinute as ReturnType<typeof vi.fn>).mockResolvedValue(MINUTE_MISSING);
    (api.stocksResearch as ReturnType<typeof vi.fn>).mockResolvedValue({ ...RESEARCH_OK, data: { as_of: null, signals: [], orders: [], positions: [] } });
    renderWithProviders(<App />, { route: '/stocks/999999.SZ' });
    expect(await screen.findByText(/尚未补跑历史行情/)).toBeInTheDocument();
    expect(screen.queryByTestId('mock-kline')).not.toBeInTheDocument();
  });

  it('history API 错误时显示错误且不影响主链提示', async () => {
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    (api.stocksHistory as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    (api.stocksSnapshot as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    (api.stocksMinute as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    (api.stocksResearch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    renderWithProviders(<App />, { route: '/stocks/600519.SH' });
    expect(await screen.findByText(/无法读取该标的行情/)).toBeInTheDocument();
  });
});
