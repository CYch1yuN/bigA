import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { api } from '../api/client';
import { renderWithProviders } from '../test/render';

vi.mock('../components/EChartsKLine', () => ({
  EChartsKLine: () => <div data-testid="mock-kline" />,
  mapTradeMarkers: () => [],
}));
vi.mock('../components/EChartsMinuteChart', () => ({
  EChartsMinuteChart: () => <div data-testid="mock-minute" />,
}));

const DEEP_API_KEYS = vi.hoisted(() => ['stocksFundamentals', 'stocksOwnership', 'stocksFunds',
  'stocksIntel', 'stocksEvents', 'stocksTechnical']);

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {};
  for (const k of ['health', 'login', 'logout', 'session', 'changePassword', 'safety',
    'dashboardSnapshot', 'prepareAction', 'executeAction', 'jobsList', 'jobGet',
    'jobPrepare', 'jobCreate', 'westockConnection', 'westockRefresh', 'stocksList',
    'stocksHistory', 'stocksSnapshot', 'stocksMinute', 'stocksResearch', ...DEEP_API_KEYS]) {
    apiMocks[k] = vi.fn();
  }
  return { ...actual, api: apiMocks };
});

const BASE = {
  ok: true, schema_version: 1, symbol: '600519.SH', source: 'westock-mcp',
  as_of: '2026-07-31', fetched_at: '2026-08-03T04:00:00Z', cache_status: 'fresh',
  is_realtime: false, transport: 'cache_export', warnings: [],
};

async function renderDetail() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  (api.stocksHistory as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true, schema_version: 1, symbol: '600519.SH', source: 'local-curated', as_of: '2026-07-31',
    fetched_at: null, cache_status: 'available', is_realtime: false, transport: 'local-curated',
    availability: { curated: true, qfq: true }, adjustment: 'qfq', range: 'all',
    data: { rows: [{ date: '2026-07-31', open: '1330.03', high: '1355.72', low: '1325.77', close: '1350.60', volume: 1, amount: 1 }] },
    warnings: [], message: 'ok',
  });
  (api.stocksSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true, schema_version: 1, symbol: '600519.SH', source: 'local', as_of: '2026-07-31',
    fetched_at: null, cache_status: 'available', is_realtime: false, transport: 'local',
    availability: { curated: true, westock_quote: false },
    data: { local: { date: '2026-07-31', close: '1350.60', open: '1', high: '1', low: '1', volume: 1, amount: 1, change: '-1', change_percent: '-0.1' }, westock_quote: null },
    warnings: [],
  });
  (api.stocksMinute as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true, schema_version: 1, symbol: '600519.SH', source: 'westock', as_of: null,
    fetched_at: null, cache_status: 'unavailable', is_realtime: false, transport: 'westock',
    availability: { westock_minute: false }, data: null, warnings: [],
  });
  (api.stocksResearch as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true, schema_version: 1, symbol: '600519.SH', source: 'biga', as_of: '2026-07-31',
    fetched_at: null, cache_status: 'available', is_realtime: false, transport: 'biga',
    availability: { artifacts: true },
    data: { as_of: '2026-07-31', signals: [], orders: [], positions: [{ account_id: 'paper-steady', symbol: '600519.SH', total_quantity: 100, sellable_quantity: 100, avg_raw_cost: '1300.00' }] },
    warnings: [],
  });
  renderWithProviders(<App />, { route: '/stocks/600519.SH' });
  await screen.findByText('600519.SH');
}

function mockDeep() {
  (api.stocksFundamentals as ReturnType<typeof vi.fn>).mockResolvedValue({
    ...BASE,
    availability: { profile: 'fresh', financials: 'fresh', forecast: 'unavailable' },
    capability_meta: {
      profile: { status: 'fresh', as_of: '2026-07-31', fetched_at: '2026-08-03T04:00:00Z', cache_age_seconds: 30 },
      financials: { status: 'fresh', as_of: '2026-07-31', fetched_at: '2026-08-03T04:00:00Z', cache_age_seconds: 30 },
      forecast: { status: 'unavailable', as_of: null, fetched_at: null, cache_age_seconds: null },
    },
    data: {
      profile: { name: '贵州茅台', industry: '白酒', business: '茅台酒生产销售' },
      financials: { summary: { report_date: '2026-06-30', revenue: 8.7e10, roe: 16.2 } },
      forecast: null,
    },
  });
}

describe('股票详情页：页面级 Tab 懒加载（Phase C）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('初始只请求行情 API，六个深度 API 调用次数全为 0', async () => {
    await renderDetail();
    expect(api.stocksHistory).toHaveBeenCalled();
    expect(api.stocksSnapshot).toHaveBeenCalled();
    expect(api.stocksMinute).toHaveBeenCalled();
    expect(api.stocksResearch).toHaveBeenCalled();
    for (const key of DEEP_API_KEYS) {
      expect((api as unknown as Record<string, ReturnType<typeof vi.fn>>)[key]).not.toHaveBeenCalled();
    }
  });

  it('点击基本面 Tab 才首次请求 fundamentals', async () => {
    mockDeep();
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    expect(await screen.findByText('贵州茅台')).toBeInTheDocument();
    expect(api.stocksFundamentals).toHaveBeenCalledTimes(1);
    expect(api.stocksOwnership).not.toHaveBeenCalled();
  });

  it('返回行情 Tab 不再重复请求深度 API', async () => {
    mockDeep();
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    await screen.findByText('贵州茅台');
    const calls = (api.stocksFundamentals as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '行情' }));
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    await screen.findByText('贵州茅台');
    // staleTime 5 分钟内返回不重复请求
    expect((api.stocksFundamentals as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls);
  });

  it('中文标签与单位，无原始 JSON 展示', async () => {
    mockDeep();
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    expect(await screen.findByText('公司名称')).toBeInTheDocument();
    expect(screen.getByText('所属行业')).toBeInTheDocument();
    expect(screen.getAllByText('营业收入').length).toBeGreaterThan(0);
    expect(screen.getByText(/亿元/)).toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\{"/)).not.toBeInTheDocument();
  });

  it('卡片独立显示 fresh/stale 元数据', async () => {
    mockDeep();
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    expect((await screen.findAllByText(/数据日期 2026-07-31/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/缓存年龄 30 秒/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('新鲜').length).toBeGreaterThan(0);
    expect(screen.getAllByText('无缓存').length).toBeGreaterThan(0); // forecast 独立降级
  });

  it('长摘要折叠提供展开/收起', async () => {
    (api.stocksIntel as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { news: 'fresh', reports: 'fresh', announcements: 'fresh' },
      capability_meta: { news: null, reports: null, announcements: null },
      data: { items: [{ category: 'news', title: '新闻', date: '2026-07-31',
                        summary: '长摘要'.repeat(60) }], total: 1,
               news: [], reports: [], announcements: [] },
      warnings: [],
    });
    mockDeep();
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '资讯公告' }));
    fireEvent.click(await screen.findByRole('button', { name: '展开' }));
    expect(screen.getByRole('button', { name: '收起' })).toBeInTheDocument();
  });

  it('intel 链接 rel="noopener noreferrer"', async () => {
    (api.stocksIntel as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { news: 'fresh', reports: 'fresh', announcements: 'fresh' },
      capability_meta: { news: null, reports: null, announcements: null },
      data: { items: [{ category: 'news', title: '新闻', date: '2026-07-31', url: 'https://example.com/a' }], total: 1, news: [], reports: [], announcements: [] },
      warnings: [],
    });
    mockDeep();
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '资讯公告' }));
    const link = await screen.findByRole('link', { name: '查看原文' });
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('部分能力失败仅对应卡片降级', async () => {
    (api.stocksFundamentals as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { profile: 'fresh', financials: 'unavailable', forecast: 'unavailable' },
      capability_meta: {
        profile: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 5 },
        financials: null, forecast: null,
      },
      data: { profile: { name: '贵州茅台' }, financials: null, forecast: null },
      warnings: ['financials 缓存结构无法标准化，已降级为不可用'],
    });
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    expect(await screen.findByText('贵州茅台')).toBeInTheDocument(); // profile 正常
    expect(screen.getByText('暂无财务数据。')).toBeInTheDocument();  // financials 降级
    expect(screen.getByText(/financials 缓存结构无法标准化/)).toBeInTheDocument();
  });

  it('财务摘要展示三张报表中文字段与金额单位', async () => {
    (api.stocksFundamentals as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { profile: 'unavailable', financials: 'fresh', forecast: 'unavailable' },
      capability_meta: {
        profile: null,
        financials: { status: 'fresh', as_of: '2026-06-30', fetched_at: null, cache_age_seconds: 60 },
        forecast: null,
      },
      data: {
        profile: null,
        financials: {
          summary: { report_date: '2026-06-30', revenue: 8.7e10 },
          income_statement: { revenue: 8.7e10, cost: 2e10, operating_profit: 4e10, total_profit: 4.1e10, net_profit: 3e10 },
          balance_sheet: { total_assets: 2.5e11, total_liabilities: 5e10, equity: 2e11, cash: 8e10, accounts_receivable: 1e9 },
          cash_flow: { operating_cash_flow: 6e10, investing_cash_flow: -2e10, financing_cash_flow: -1e10, net_cash_flow: 3e10 },
        },
        forecast: null,
      },
      warnings: [],
    });
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    expect(await screen.findByText('利润表摘要')).toBeInTheDocument();
    expect(screen.getByText('营业成本')).toBeInTheDocument();
    expect(screen.getByText('利润总额')).toBeInTheDocument();
    expect(screen.getByText('资产负债表摘要')).toBeInTheDocument();
    expect(screen.getByText('货币资金')).toBeInTheDocument();
    expect(screen.getByText('应收账款')).toBeInTheDocument();
    expect(screen.getByText('现金流量表摘要')).toBeInTheDocument();
    expect(screen.getByText('现金净增加额')).toBeInTheDocument();
    expect(screen.getAllByText(/亿元/).length).toBeGreaterThan(0); // 金额单位
  });

  it('北向持股卡片展示数量/比例/变化与元数据', async () => {
    (api.stocksFunds as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { margin: 'unavailable', block_trade: 'unavailable', fund_flow: 'unavailable', northbound: 'fresh', lhb: 'unavailable', chip_distribution: 'unavailable' },
      capability_meta: {
        northbound: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 45 },
      },
      data: {
        margin: null, block_trade: null, fund_flow: null,
        northbound: {
          unit_note: '单位说明：持股数量为股，持股市值为元，比例为 %。',
          current: { date: '2026-06-30', holding_shares: 8.5e7, holding_ratio: 6.8,
                     holding_cap: 1.2e11, shares_change_q: -2e6, shares_change_y: 3e6,
                     cap_change_q: -5e8, cap_change_y: 8e8 },
          previous: { date: '2026-03-31', holding_shares: 8.7e7, holding_ratio: 6.9,
                      holding_cap: 1.25e11, shares_change_q: 1e6, shares_change_y: 2e6,
                      cap_change_q: 3e8, cap_change_y: 6e8 },
        },
        lhb: null, chip_distribution: null,
      },
      warnings: [],
    });
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '资金' }));
    expect(await screen.findByText('北向持股')).toBeInTheDocument();
    expect(screen.getByText('本期')).toBeInTheDocument();
    expect(screen.getByText('上期')).toBeInTheDocument();
    expect(screen.getAllByText('持股数量').length).toBeGreaterThanOrEqual(2); // 本期+上期
    expect(screen.getAllByText(/万股/).length).toBeGreaterThan(0); // 8.5e7 → 8500 万股
    expect(screen.getAllByText('6.80%').length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText('持股变化')).not.toBeInTheDocument(); // 旧字段不再读取
    expect(screen.getByText(/单位说明/)).toBeInTheDocument();
    expect(screen.getByText('2026-06-30')).toBeInTheDocument();
  });

  it('Intel 三分类中文元数据；category 切换只显示当前分类', async () => {
    (api.stocksIntel as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { news: 'stale', reports: 'stale', announcements: 'stale' },
      capability_meta: { news: null, reports: null, announcements: null },
      data: {
        items: [{ category: 'news', title: '新闻', date: '2026-07-31' }], total: 1,
        news: [], reports: [], announcements: [],
      },
      warnings: [],
    });
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '资讯公告' }));
    // 全部分类：三组中文元数据
    await waitFor(() => expect(screen.getAllByText('新闻').length).toBeGreaterThan(0), { timeout: 3000 });
    expect(screen.getAllByText('研报').length).toBeGreaterThan(0);
    expect(screen.getAllByText('公告').length).toBeGreaterThan(0);
    // 筛选按钮为中文，无英文按钮文案
    expect(screen.getByRole('button', { name: '新闻' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '研报' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '公告' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '全部' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'news' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'reports' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'announcements' })).not.toBeInTheDocument();
    // category 切换：按钮中文，内部 API 参数保持英文
    fireEvent.click(screen.getByRole('button', { name: '新闻' }));
    await waitFor(() => expect(api.stocksIntel).toHaveBeenCalledWith('600519.SH', { category: 'news', limit: 10, offset: 0 }));
    await waitFor(() => expect(screen.getAllByText('新闻').length).toBeGreaterThan(0), { timeout: 3000 });
    // 元数据行只显示当前分类（deep-status-item 内 badge 仅剩“新闻”）
    await waitFor(() => {
      const badges = [...document.querySelectorAll('.deep-status-item .badge')].map((b) => b.textContent);
      expect(badges).toEqual(['新闻']);
    }, { timeout: 3000 });
  });

  it('资讯筛选按钮中文且内部 category 参数保持英文协议', async () => {
    (api.stocksIntel as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { news: 'fresh', reports: 'fresh', announcements: 'fresh' },
      capability_meta: { news: null, reports: null, announcements: null },
      data: {
        items: [], total: 0,
        news: [], reports: [], announcements: [],
      },
      warnings: [],
    });
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '资讯公告' }));
    await waitFor(() => expect(api.stocksIntel).toHaveBeenCalledWith('600519.SH', { category: undefined, limit: 10, offset: 0 }));
    // 点击中文“研报” → API 参数为 reports
    fireEvent.click(await screen.findByRole('button', { name: '研报' }));
    await waitFor(() => expect(api.stocksIntel).toHaveBeenCalledWith('600519.SH', { category: 'reports', limit: 10, offset: 0 }));
    // 点击中文“公告” → API 参数为 announcements
    fireEvent.click(await screen.findByRole('button', { name: '公告' }));
    await waitFor(() => expect(api.stocksIntel).toHaveBeenCalledWith('600519.SH', { category: 'announcements', limit: 10, offset: 0 }));
    // 无用户可见英文按钮
    expect(screen.queryByRole('button', { name: 'news' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'reports' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'announcements' })).not.toBeInTheDocument();
  });

  it('技术指标展示固定安全文案，无策略按钮', async () => {
    (api.stocksTechnical as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { technical: 'fresh' },
      capability_meta: { technical: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 10 } },
      data: { indicators: { ma: { ma5: 1360.0 }, date: '2026-07-31' }, note: undefined },
      warnings: [],
    });
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '技术指标' }));
    expect(await screen.findByText(/BigA 策略与回测使用本地 curated 数据独立计算/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /买入|卖出|下单/ })).not.toBeInTheDocument();
  });

  it('不出现 [object Object]/原始 JSON/NaN/undefined', async () => {
    mockDeep();
    await renderDetail();
    fireEvent.click(screen.getByRole('button', { name: '基本面' }));
    await screen.findByText('贵州茅台');
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\{"/)).not.toBeInTheDocument();
    expect(screen.queryByText('NaN')).not.toBeInTheDocument();
  });
});
