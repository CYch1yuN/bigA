import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { api } from '../api/client';
import { renderWithProviders } from '../test/render';

const echartsMock = vi.hoisted(() => {
  const setOption = vi.fn(); const resize = vi.fn(); const dispose = vi.fn();
  const init = vi.fn(() => ({ setOption, resize, dispose }));
  return { init, setOption, resize, dispose };
});
vi.mock('echarts', () => ({ init: echartsMock.init }));

const MARKET_KEYS = vi.hoisted(() => ['marketOverview', 'marketDistribution', 'marketHot', 'marketFunds',
  'marketMacro', 'marketCalendar', 'marketEvents']);

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {};
  const keys = ['health', 'login', 'logout', 'session', 'changePassword', 'safety',
    'dashboardSnapshot', 'prepareAction', 'executeAction', 'jobsList', 'jobGet',
    'jobPrepare', 'jobCreate', 'westockConnection', 'westockRefresh', 'stocksList',
    'stocksHistory', 'stocksSnapshot', 'stocksMinute', 'stocksResearch',
    'stocksFundamentals', 'stocksOwnership', 'stocksFunds', 'stocksIntel',
    'stocksEvents', 'stocksTechnical', 'marketSectors', 'marketIndexes',
    'marketConstituents', 'marketIndustryChain', ...MARKET_KEYS];
  for (const k of keys) apiMocks[k] = vi.fn();
  return { ...actual, api: apiMocks };
});

const BASE = {
  ok: true, schema_version: 1, source: 'westock-mcp', as_of: '2026-07-31',
  fetched_at: '2026-08-03T04:00:00Z', cache_status: 'fresh', is_realtime: false,
  transport: 'cache_export', warnings: [],
};

async function renderMarket() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  (api.marketOverview as ReturnType<typeof vi.fn>).mockResolvedValue({
    ...BASE,
    availability: { market_overview: 'fresh' },
    capability_meta: { market_overview: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 30 } },
    data: { overview: { score: 62.5, risk_level: '中', summary: '市场情绪偏暖', dimensions: { trend: 60, sentiment: 55, liquidity: 70, breadth: 58, volatility: 40, risk: 45 } } },
  });
  (api.marketDistribution as ReturnType<typeof vi.fn>).mockResolvedValue({
    ...BASE,
    availability: { change_distribution: 'fresh' },
    capability_meta: { change_distribution: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 30 } },
    data: { distribution: { rise_count: 2800, fall_count: 1900, flat_count: 300, limit_up_count: 45, limit_down_count: 3, total_amount: 8.2e11, bins: [{ label: '0-1%', count: 900 }] } },
  });
  renderWithProviders(<App />, { route: '/market' });
  await screen.findByText('市场研究中心');
}

describe('市场研究中心（Phase D）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('默认 Tab 只请求 overview+distribution，其他市场 API 为 0', async () => {
    await renderMarket();
    expect(api.marketOverview).toHaveBeenCalledTimes(1);
    expect(api.marketDistribution).toHaveBeenCalledTimes(1);
    for (const key of ['marketHot', 'marketFunds', 'marketMacro', 'marketCalendar', 'marketEvents', 'marketSectors', 'marketIndexes']) {
      expect((api as unknown as Record<string, ReturnType<typeof vi.fn>>)[key]).not.toHaveBeenCalled();
    }
  });

  it('点击 Tab 才首次请求对应 API', async () => {
    (api.marketHot as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { hot_ranking: 'fresh' }, capability_meta: {},
      data: { hot: { stocks: [{ rank: 1, symbol: '600519.SH', name: '贵州茅台', price: 1350, change_percent: 3.2, heat: 95, reason: '业绩超预期' }], sectors: [] } },
    });
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '热点排行' }));
    expect(await screen.findByText('贵州茅台')).toBeInTheDocument();
    expect(api.marketHot).toHaveBeenCalledTimes(1);
  });

  it('返回旧 Tab 不重复请求（staleTime）', async () => {
    (api.marketHot as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { hot_ranking: 'fresh' }, capability_meta: {},
      data: { hot: { stocks: [{ rank: 1, symbol: '600519.SH', name: '贵州茅台', price: 1350, change_percent: 3.2, heat: 95, reason: 'r' }], sectors: [] } },
    });
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '热点排行' }));
    await screen.findByText('贵州茅台');
    const calls = (api.marketHot as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '市场概览' }));
    fireEvent.click(screen.getByRole('button', { name: '热点排行' }));
    await screen.findByText('贵州茅台');
    expect((api.marketHot as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls);
  });

  it('全中文标签与固定安全文案，无原始 JSON/NaN/undefined', async () => {
    await renderMarket();
    expect(await screen.findByText('市场评分')).toBeInTheDocument();
    expect(screen.getByText(/不直接生成 BigA 信号、订单或持仓/)).toBeInTheDocument();
    expect(screen.getByText(/上涨 2,800 家/)).toBeInTheDocument();
    expect(screen.getByText('风险等级：中')).toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\{"/)).not.toBeInTheDocument();
    expect(screen.queryByText('NaN')).not.toBeInTheDocument();
  });

  it('无买入/卖出/下单/生成信号按钮', async () => {
    await renderMarket();
    expect(screen.queryByRole('button', { name: /买入|卖出|下单|生成信号/ })).not.toBeInTheDocument();
  });

  it('市场事件与日历 Tab 展示中文与来源提示', async () => {
    (api.marketEvents as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { events: 'fresh' }, capability_meta: {},
      data: { events: [{ category: 'events', date: '2026-08-01', title: '限售股解禁', severity: '中', summary: '占总股本0.5%', symbols: ['600519.SH'] }], total: 1 },
      warnings: ['市场数据来自 Westock 缓存'],
    });
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '市场事件' }));
    expect(await screen.findByText('限售股解禁')).toBeInTheDocument();
    expect(screen.getByText('事件')).toBeInTheDocument();
    expect(screen.getAllByText(/不直接生成 BigA 信号/).length).toBeGreaterThan(0);
  });

  it('板块页行业/概念切换与本地无伪造', async () => {
    (api.marketSectors as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { sector: 'fresh' }, capability_meta: {},
      data: { sectors: [
        { code: 'BK01', name: '白酒', sector_type: 'concept', change_percent: 2.1, amount: 3e10, turnover_rate: 1.5, rise_count: 18, fall_count: 2 },
        { code: 'BK02', name: '银行', sector_type: 'industry', change_percent: 0.3, amount: 2e10, turnover_rate: 0.4, rise_count: 20, fall_count: 10 },
      ] },
    });
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    renderWithProviders(<App />, { route: '/market/sectors' });
    expect(await screen.findByText('白酒')).toBeInTheDocument();
    expect(screen.getByText('银行')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '概念' }));
    await waitFor(() => expect(screen.queryByText('银行')).not.toBeInTheDocument());
    expect(screen.getByText('白酒')).toBeInTheDocument();
  });

  it('指数页成分股本地可用/尚未补跑标识', async () => {
    (api.marketIndexes as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { index: 'fresh' }, capability_meta: {},
      data: { indexes: [{ code: '000001.SH', name: '上证指数', price: 3450, change: 12.5, change_percent: 0.36, amount: 5.1e11, volume: 4.2e10 }] },
    });
    (api.marketConstituents as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { index: 'fresh' }, capability_meta: {},
      data: { index_code: '000001.SH', constituents: [
        { symbol: '600519.SH', name: '贵州茅台', weight: 5.2, industry: '白酒', local_history_available: true },
        { symbol: '999999.SZ', name: '无数据股', weight: 0.1, industry: '未知', local_history_available: false },
      ] },
    });
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    renderWithProviders(<App />, { route: '/market/indexes' });
    fireEvent.click(await screen.findByRole('button', { name: '成分股' }));
    expect(await screen.findByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('本地数据可用')).toBeInTheDocument();
    expect(screen.getByText('尚未补跑')).toBeInTheDocument();
    // 本地可用才可链接到详情
    expect(screen.getAllByRole('link', { name: '查看' }).length).toBe(1);
    expect(api.marketConstituents).toHaveBeenCalledWith('000001.SH');
  });
});
