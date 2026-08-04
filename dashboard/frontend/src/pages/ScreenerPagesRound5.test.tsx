import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import App from '../App';
import { api } from '../api/client';
import { renderWithProviders } from '../test/render';
import { fmtIsoTime } from '../components/StrongCards';

const SCREENER_KEYS = vi.hoisted(() => ['screenerRun', 'screenerResult', 'screenerSavedList',
  'screenerSavedCreate', 'screenerSavedDelete', 'screenerCandidatesList',
  'screenerCandidatesAdd', 'screenerCandidatesDelete', 'marketSectors',
  'marketIndexes', 'marketIndustryChain']);

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  const apiMocks: Record<string, ReturnType<typeof vi.fn>> = {};
  const keys = ['health', 'login', 'logout', 'session', 'changePassword', 'safety',
    'dashboardSnapshot', 'prepareAction', 'executeAction', 'jobsList', 'jobGet',
    'jobPrepare', 'jobCreate', 'westockConnection', 'westockRefresh', 'stocksList',
    'stocksHistory', 'stocksSnapshot', 'stocksMinute', 'stocksResearch',
    'stocksFundamentals', 'stocksOwnership', 'stocksFunds', 'stocksIntel',
    'stocksEvents', 'stocksTechnical', 'marketOverview', 'marketDistribution',
    'marketHot', 'marketFunds', 'marketMacro', 'marketCalendar', 'marketEvents', ...SCREENER_KEYS];
  for (const k of keys) apiMocks[k] = vi.fn();
  return { ...actual, api: apiMocks };
});

const LONG_SCOPE = 'q_' + 'a'.repeat(64);
const RUN_RESULT = {
  ok: true, schema_version: 1, result_id: 'a'.repeat(32), mode: 'condition',
  source: 'westock-mcp', as_of: '2026-07-31',
  source_fetched_at: '2026-08-03T16:29:36+00:00',  // UTC 跨日 → 上海 08-04 00:29:36
  generated_at: '2026-08-03T16:29:36+00:00',
  cache_status: 'fresh', is_realtime: false, transport: 'cache_export',
  availability: { filter: 'fresh' },
  query: { mode: 'condition', universe: { type: 'local', value: null }, conditions: [],
    strategy: null, factor: null, labels: null, sort: { field: 'score', direction: 'desc' }, limit: 50 },
  data: { items: [
    { symbol: '600519.SH', name: '贵州茅台', score: 90, rank: 1, price: 1350, change_percent: 3.2, industry: '白酒', reason: '业绩超预期', local_history_available: true },
  ], total: 1 },
  warnings: ['当前没有与该筛选条件精确匹配的 Westock 缓存导出，未执行实时查询。'],
  cache_scope: LONG_SCOPE,
};

async function renderResultPage(payload: typeof RUN_RESULT) {
  (api.screenerResult as ReturnType<typeof vi.fn>).mockResolvedValue(payload);
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  renderWithProviders(<App />, { route: '/screener/results/' + 'a'.repeat(32) });
  await screen.findByText('贵州茅台');
}

describe('选股中心 Phase E 第五轮：时间时区与移动端换行', () => {
  beforeEach(() => vi.clearAllMocks());

  it('fmtIsoTime：UTC 跨日 → 上海时间（2026-08-03T16:29:36+00:00 → 2026-08-04 00:29:36）', () => {
    expect(fmtIsoTime('2026-08-03T16:29:36+00:00')).toBe('2026-08-04 00:29:36');
  });

  it('fmtIsoTime：+08:00 时间不重复偏移', () => {
    expect(fmtIsoTime('2026-08-04T00:29:36+08:00')).toBe('2026-08-04 00:29:36');
  });

  it('fmtIsoTime：null / 非法值 → —', () => {
    expect(fmtIsoTime(null)).toBe('—');
    expect(fmtIsoTime(undefined)).toBe('—');
    expect(fmtIsoTime('not-a-time')).toBe('—');
    expect(fmtIsoTime('')).toBe('—');
  });

  it('结果页时间按上海时区展示，不使用 slice(0,19) 直出', async () => {
    await renderResultPage(RUN_RESULT);
    // 抓取/生成显示上海时间
    expect(screen.getAllByText(/2026-08-04 00:29:36/).length).toBeGreaterThan(0);
    // 不得出现 naive slice 直出的 UTC 字面（2026-08-03 16:29:36）
    expect(screen.queryByText(/2026-08-03 16:29:36/)).not.toBeInTheDocument();
    // as_of 仍按日期展示，不做时区转换
    expect(screen.getAllByText(/数据日期 2026-07-31/).length).toBeGreaterThan(0);
  });

  it('cache_scope 完整保留不截断', async () => {
    await renderResultPage(RUN_RESULT);
    // 缓存范围完整 66 字符
    expect(screen.getAllByText(new RegExp(LONG_SCOPE)).length).toBeGreaterThan(0);
    // 完整 q_ 前缀 + 64 hex
    const full = 'q_' + 'a'.repeat(64);
    expect(screen.getAllByText(new RegExp(full.replace(/a/g, '[a-f0-9]'))).length).toBeGreaterThan(0);
  });

  it('长 cache_scope 与长 warning 对应元素具有可换行样式/class', async () => {
    await renderResultPage(RUN_RESULT);
    // cache_scope chip：deep-chip class（携带 overflow-wrap:anywhere / min-width:0 / max-width:100%）
    const chip = screen.getAllByText(new RegExp(LONG_SCOPE)).map((el) => el.closest('.deep-chip')).find(Boolean);
    expect(chip).toBeTruthy();
    // warning alert：alert-warning class（携带 overflow-wrap:anywhere）
    const alert = screen.getAllByText(/当前没有与该筛选条件精确匹配/).map((el) => el.closest('.alert-warning')).find(Boolean);
    expect(alert).toBeTruthy();
    // 页面无隐藏截断：完整 scope 文本可见
    expect(screen.getAllByText(new RegExp(LONG_SCOPE)).length).toBeGreaterThan(0);
  });

  it('已保存条件/研究候选时间统一用 fmtIsoTime（上海时区）', async () => {
    (api.screenerSavedList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { id: 'b'.repeat(32), name: '条件', query: RUN_RESULT.query,
        created_at: '2026-08-03T16:29:36+00:00', updated_at: '2026-08-03T16:29:36+00:00' },
    ] });
    (api.screenerCandidatesList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { symbol: '600519.SH', name: '贵州茅台', source_result_id: 'a'.repeat(32), note: '',
        added_at: '2026-08-03T16:29:36+00:00', local_history_available: true },
    ], note: '研究候选列表仅用于人工研究整理，不会生成 BigA 信号、订单或持仓，也不会同步到 Westock 自选股。' });
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    renderWithProviders(<App />, { route: '/screener' });
    await screen.findByRole('button', { name: '条件选股' });
    fireEvent.click(screen.getByRole('button', { name: '已保存条件' }));
    expect((await screen.findAllByText(/2026-08-04 00:29:36/)).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: '研究候选' }));
    expect((await screen.findAllByText(/2026-08-04 00:29:36/)).length).toBeGreaterThan(0);
  });
});
