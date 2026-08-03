import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import App from '../App';
import { api } from '../api/client';
import { renderWithProviders } from '../test/render';

vi.mock('echarts', () => {
  const h = {
    setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
    init: vi.fn(() => ({ setOption: h.setOption, resize: h.resize, dispose: h.dispose })),
  };
  return { init: h.init };
});

const MARKET_KEYS = vi.hoisted(() => ['marketOverview', 'marketDistribution', 'marketHot', 'marketFunds',
  'marketMacro', 'marketCalendar', 'marketEvents', 'marketSectors', 'marketIndexes',
  'marketConstituents', 'marketIndustryChain']);

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {};
  const keys = ['health', 'login', 'logout', 'session', 'changePassword', 'safety',
    'dashboardSnapshot', 'prepareAction', 'executeAction', 'jobsList', 'jobGet',
    'jobPrepare', 'jobCreate', 'westockConnection', 'westockRefresh', 'stocksList',
    'stocksHistory', 'stocksSnapshot', 'stocksMinute', 'stocksResearch',
    'stocksFundamentals', 'stocksOwnership', 'stocksFunds', 'stocksIntel',
    'stocksEvents', 'stocksTechnical', ...MARKET_KEYS];
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
  renderWithProviders(<App />, { route: '/market' });
  await screen.findByText('市场研究中心');
}

function mockOverview(ok: boolean) {
  (api.marketOverview as ReturnType<typeof vi.fn>).mockResolvedValue(ok ? {
    ...BASE,
    availability: { market_overview: 'fresh' },
    capability_meta: { market_overview: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 30 } },
    data: { overview: { score: 62.5, risk_level: '中', summary: '市场情绪偏暖', dimensions: { trend: 60 } } },
  } : Promise.reject(new Error('boom')));
}

function mockDistribution(ok: boolean) {
  (api.marketDistribution as ReturnType<typeof vi.fn>).mockResolvedValue(ok ? {
    ...BASE,
    availability: { change_distribution: 'fresh' },
    capability_meta: { change_distribution: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 30 } },
    data: { distribution: { rise_count: 2800, fall_count: 1900, bins: [{ label: '0-1%', count: 900 }] } },
  } : Promise.reject(new Error('boom')));
}

describe('市场研究中心 Codex 修正（Phase D）', () => {
  beforeEach(() => vi.clearAllMocks());

  // ---- 产业链懒加载 ----
  it('产业链初始 0 请求；点击 Tab 首次请求；返回不重复', async () => {
    mockOverview(true);
    mockDistribution(true);
    (api.marketIndustryChain as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { industry_chain: 'fresh' },
      capability_meta: { industry_chain: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 60 } },
      data: { chains: [
        { code: 'IC01', name: '白酒产业链', description: '从高粱种植到终端销售',
          upstream: [{ code: 'N1', name: '高粱种植', node_type: '上游', related_symbols: ['000001.SZ'] }],
          midstream: [{ code: 'N2', name: '酿造', node_type: '中游', related_symbols: ['600519.SH'] }],
          downstream: [{ code: 'N3', name: '经销', node_type: '下游', related_symbols: [] }] },
      ] },
    });
    await renderMarket();
    expect(api.marketIndustryChain).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '产业链' }));
    expect(await screen.findByText('白酒产业链')).toBeInTheDocument();
    expect(api.marketIndustryChain).toHaveBeenCalledTimes(1);
    const calls = (api.marketIndustryChain as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.click(screen.getByRole('button', { name: '市场概览' }));
    fireEvent.click(screen.getByRole('button', { name: '产业链' }));
    await screen.findByText('白酒产业链');
    expect((api.marketIndustryChain as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls);
  });

  it('产业链三阶段节点受控展示，related_symbols 为纯文本代码', async () => {
    mockOverview(true);
    mockDistribution(true);
    (api.marketIndustryChain as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { industry_chain: 'fresh' }, capability_meta: {},
      data: { chains: [
        { code: 'IC01', name: '白酒产业链', description: '说明文本',
          upstream: [{ code: 'N1', name: '高粱种植', node_type: '上游', related_symbols: ['000001.SZ', 'bad-sym'] }],
          midstream: [], downstream: [] },
      ] },
    });
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '产业链' }));
    expect(await screen.findByText('白酒产业链')).toBeInTheDocument();
    expect(screen.getByText('上游')).toBeInTheDocument();
    expect(screen.getByText('中游')).toBeInTheDocument();
    expect(screen.getByText('下游')).toBeInTheDocument();
    expect(screen.getByText(/关联：000001.SZ/)).toBeInTheDocument();
    // related_symbols 不产生个股链接（纯文本代码）
    expect(screen.queryByRole('link', { name: /000001.SZ/ })).not.toBeInTheDocument();
  });

  // ---- 单能力失败降级（4 场景） ----
  it('overview 失败 + distribution 成功：分布仍显示', async () => {
    mockOverview(false);
    mockDistribution(true);
    await renderMarket();
    expect(await screen.findByText(/无法读取市场评分/)).toBeInTheDocument();
    expect(screen.getByText(/上涨 2,800 家/)).toBeInTheDocument();
    expect(screen.getByText('涨跌家数')).toBeInTheDocument();
  });

  it('distribution 失败 + overview 成功：评分仍显示', async () => {
    mockOverview(true);
    mockDistribution(false);
    await renderMarket();
    expect(await screen.findByText('市场评分')).toBeInTheDocument();
    expect(screen.getByText(/无法读取涨跌分布/)).toBeInTheDocument();
  });

  it('macro 失败 + calendar 成功：日历仍显示', async () => {
    mockOverview(true);
    mockDistribution(true);
    (api.marketMacro as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    (api.marketCalendar as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { events: 'fresh', announcements: 'fresh' },
      capability_meta: { events: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 10 }, announcements: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 10 } },
      data: { items: [{ category: 'events', date: '2026-08-01', title: '限售股解禁', importance: 'high' }], total: 1 },
      warnings: ['财经日历由 Westock 事件与公告缓存派生，并非独立财经日历能力；actual、forecast、previous 仅在来源数据明确提供时展示。'],
    });
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '宏观日历' }));
    expect(await screen.findByText('限售股解禁')).toBeInTheDocument();
    expect(screen.getByText(/无法读取宏观指标/)).toBeInTheDocument();
    // 派生日历边界说明可见
    expect(screen.getAllByText(/并非独立财经日历能力/).length).toBeGreaterThan(0);
    // 来源中文标识 + 独立元数据
    expect(screen.getAllByText('事件').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/公告来源/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/数据日期 2026-07-31/).length).toBeGreaterThan(0);
  });

  it('calendar 失败 + macro 成功：宏观仍显示', async () => {
    mockOverview(true);
    mockDistribution(true);
    (api.marketMacro as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { macro: 'fresh' },
      capability_meta: { macro: { status: 'fresh', as_of: '2026-07-31', fetched_at: null, cache_age_seconds: 5 } },
      data: { indicators: [{ code: 'M1', name: 'CPI 同比', value: 0.8, unit: '%', period: '2026-06', importance: 'high' }] },
    });
    (api.marketCalendar as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '宏观日历' }));
    expect(await screen.findByText('CPI 同比')).toBeInTheDocument();
    expect(screen.getByText(/无法读取财经日历/)).toBeInTheDocument();
  });

  // ---- 非本地标的无个股链接 ----
  it('热门股票仅本地可用才链接；非本地显示尚未补跑', async () => {
    mockOverview(true);
    mockDistribution(true);
    (api.marketHot as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { hot_ranking: 'fresh' }, capability_meta: {},
      data: { hot: {
        stocks: [
          { rank: 1, symbol: '600519.SH', name: '本地股', price: 1350, change_percent: 3.2, heat: 95, local_history_available: true },
          { rank: 2, symbol: '999999.SZ', name: '非本地股', price: 10, change_percent: 1.0, heat: 50, local_history_available: false },
        ],
        sectors: [],
      } },
    });
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '热点排行' }));
    expect(await screen.findByText('本地股')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: '600519.SH' }).length).toBe(1); // 本地可链接
    expect(screen.queryByRole('link', { name: '999999.SZ' })).not.toBeInTheDocument(); // 非本地无链接
    expect(screen.getByText(/999999.SZ（尚未补跑）/)).toBeInTheDocument();
  });

  // ---- 无英文 capability/原始 JSON/NaN/undefined ----
  it('无英文 capability/tool 名与原始展示', async () => {
    mockOverview(true);
    mockDistribution(true);
    (api.marketFunds as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { margin: 'fresh', northbound: 'fresh' }, capability_meta: {},
      data: { funds: { margin_balance: 1.8e12, margin_change: 2e10, northbound_net: 4.5e9, northbound_holding: 6e11, southbound_net: null, date: '2026-07-31' } },
    });
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '资金' }));
    expect(await screen.findByText(/融资余额 18000.00 亿元/)).toBeInTheDocument();
    expect(screen.queryByText(/market_overview|hot_ranking|data_/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.queryByText('NaN')).not.toBeInTheDocument();
    expect(screen.queryByText(/\{"/)).not.toBeInTheDocument();
  });

  // ---- 财经日历结构化展示与来源元数据 ----
  function mockCalendarWithItems() {
    (api.marketMacro as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE, availability: { macro: 'unavailable' },
      capability_meta: { macro: { status: 'unavailable', as_of: null, fetched_at: null, cache_age_seconds: null } },
      data: { indicators: [] }, warnings: [],
    });
    (api.marketCalendar as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE,
      availability: { events: 'fresh', announcements: 'stale' },
      capability_meta: {
        events: { status: 'fresh', as_of: '2026-07-31', fetched_at: '2026-08-03T04:00:00Z', cache_age_seconds: 30 },
        announcements: { status: 'stale', as_of: '2026-07-30', fetched_at: '2026-08-02T04:00:00Z', cache_age_seconds: 99999 },
      },
      data: { items: [
        { category: 'events', date: '2026-08-01', time: '09:30', title: '限售股解禁', importance: 'high',
          country: '中国', actual: 3.2, forecast: 2.8, previous: 2.5, url: 'https://example.com/e1' },
        { category: 'announcements', date: '', time: '', title: '缺字段公告', importance: '', country: '' },
      ], total: 2 },
      warnings: ['财经日历由 Westock 事件与公告缓存派生，并非独立财经日历能力；actual、forecast、previous 仅在来源数据明确提供时展示。'],
    });
  }

  it('日历条目结构化表格：完整字段与缺字段暂无', async () => {
    mockOverview(true);
    mockDistribution(true);
    mockCalendarWithItems();
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '宏观日历' }));
    expect(await screen.findByText('限售股解禁')).toBeInTheDocument();
    // 完整条目各列
    expect(screen.getByText('09:30')).toBeInTheDocument();
    expect(screen.getByText('中国')).toBeInTheDocument();
    expect(screen.getAllByText('高').length).toBeGreaterThan(0);
    expect(screen.getAllByText('3.2').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2.8').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2.5').length).toBeGreaterThan(0);
    // 缺字段条目显示“暂无”
    expect(screen.getAllByText('暂无').length).toBeGreaterThanOrEqual(5);
    // 页面不存在 `· ·` 拼接
    expect(screen.queryByText(/·\s*·/)).not.toBeInTheDocument();
    // 无 NaN/Infinity/undefined/[object Object]
    expect(screen.queryByText(/NaN|Infinity|undefined|\[object Object\]/)).not.toBeInTheDocument();
  });

  it('日历来源元数据：中文来源名 + 独立元数据 + URL rel', async () => {
    mockOverview(true);
    mockDistribution(true);
    mockCalendarWithItems();
    await renderMarket();
    fireEvent.click(screen.getByRole('button', { name: '宏观日历' }));
    // 两组中文来源名
    expect(await screen.findByText('事件来源')).toBeInTheDocument();
    expect(screen.getByText('公告来源')).toBeInTheDocument();
    // 独立元数据：events fresh（数据日期 2026-07-31）+ announcements stale（已过期）
    expect(screen.getAllByText(/数据日期 2026-07-31/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/缓存年龄 30 秒/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('已过期').length).toBeGreaterThan(0);
    // 不显示 capability 英文标识 / MCP tool 名
    expect(screen.queryByText(/events|announcements|data_|capability/)).not.toBeInTheDocument();
    // URL rel 正确
    const link = screen.getByRole('link', { name: '查看原文' });
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
    expect(link.getAttribute('target')).toBe('_blank');
  });
});
