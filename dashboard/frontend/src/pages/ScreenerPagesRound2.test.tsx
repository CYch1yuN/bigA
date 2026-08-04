import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import App from '../App';
import { api } from '../api/client';
import { renderWithProviders } from '../test/render';

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

const BASE_RUN = {
  ok: true, schema_version: 1, result_id: 'a'.repeat(32), mode: 'condition',
  source: 'westock-mcp', as_of: '2026-07-31', source_fetched_at: '2026-08-03T04:00:00Z',
  generated_at: '2026-08-03T12:00:00Z', cache_status: 'fresh', is_realtime: false,
  transport: 'cache_export', availability: { filter: 'fresh' },
  query: { mode: 'condition', universe: { type: 'local', value: null }, conditions: [],
    strategy: null, factor: null, labels: null, sort: { field: 'score', direction: 'desc' }, limit: 50 },
  data: { items: [
    { symbol: '600519.SH', name: '贵州茅台', score: 90, rank: 1, price: 1350, change_percent: 3.2, industry: '白酒', reason: '业绩超预期', local_history_available: true },
  ], total: 1 },
  warnings: [],
  cache_scope: 'q_' + '0'.repeat(64),
};

const UNAVAILABLE_RUN = {
  ...BASE_RUN, cache_status: 'unavailable', as_of: null, source_fetched_at: null,
  availability: { filter: 'unavailable' }, data: { items: [], total: 0 },
  warnings: ['当前没有与该筛选条件精确匹配的 Westock 缓存导出，未执行实时查询。'],
  cache_scope: 'q_' + '1'.repeat(64),
};

async function renderScreener() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  renderWithProviders(<App />, { route: '/screener' });
  await screen.findByRole('button', { name: '条件选股' });
}

describe('选股中心 Phase E 第二轮回归', () => {
  beforeEach(() => vi.clearAllMocks());

  it('unavailable run 后结果页成功打开而非 404，并显示缓存导出提示', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(UNAVAILABLE_RUN);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '执行选股' }));
    await waitFor(() => expect(api.screenerRun).toHaveBeenCalled());
    expect(window.location.href).toContain('/screener/results/');
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });

    // 直接渲染结果页（模拟 GET 200，非 404）
    (api.screenerResult as ReturnType<typeof vi.fn>).mockResolvedValue(UNAVAILABLE_RUN);
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    renderWithProviders(<App />, { route: '/screener/results/' + 'a'.repeat(32) });
    // 未执行实时查询 + 导出提示 + 不进入 result_not_found
    expect((await screen.findAllByText(/未执行实时查询/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/需要通过 WorkBuddy 为当前筛选条件导出缓存/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/缓存范围/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/result_not_found/)).not.toBeInTheDocument();
  });

  it('enum/in 条件：macd_signal 多选发送数组', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(BASE_RUN);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '添加条件' }));
    fireEvent.change(screen.getByLabelText('条件字段'), { target: { value: 'macd_signal' } });
    fireEvent.change(screen.getByLabelText('条件操作符'), { target: { value: 'in' } });
    // 多选：金叉默认已勾选（emptyCondition 默认值），勾选中性
    fireEvent.click(screen.getByLabelText('中性'));
    fireEvent.click(screen.getByRole('button', { name: '执行选股' }));
    await waitFor(() => expect(api.screenerRun).toHaveBeenCalled());
    const payload = (api.screenerRun as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.conditions[0]).toEqual({ field: 'macd_signal', operator: 'in', value: ['golden_cross', 'neutral'] });
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('enum/eq 条件：单选发送标量', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(BASE_RUN);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '添加条件' }));
    fireEvent.change(screen.getByLabelText('条件字段'), { target: { value: 'macd_signal' } });
    fireEvent.change(screen.getByLabelText('条件枚举值'), { target: { value: 'death_cross' } });
    fireEvent.click(screen.getByRole('button', { name: '执行选股' }));
    await waitFor(() => expect(api.screenerRun).toHaveBeenCalled());
    const payload = (api.screenerRun as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.conditions[0]).toEqual({ field: 'macd_signal', operator: 'eq', value: 'death_cross' });
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('载入保存条件恢复表单（同模式：条件值恢复）', async () => {
    (api.screenerSavedList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { id: 'b'.repeat(32), name: '载入条件', query: {
        mode: 'condition', universe: { type: 'local', value: null },
        conditions: [{ field: 'macd_signal', operator: 'in', value: ['bullish', 'neutral'] }],
        strategy: null, factor: null, labels: null, sort: { field: 'score', direction: 'desc' }, limit: 50 },
        created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z' },
    ] });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '已保存条件' }));
    fireEvent.click(await screen.findByRole('button', { name: '载入' }));
    // 切回条件 Tab 且 enum 条件恢复
    await waitFor(() => expect(screen.getByRole('button', { name: '条件选股' })).toBeInTheDocument());
    expect((screen.getByLabelText('条件字段') as HTMLSelectElement).value).toBe('macd_signal');
    expect((screen.getByLabelText('条件操作符') as HTMLSelectElement).value).toBe('in');
    expect((screen.getByLabelText('看多') as HTMLInputElement).checked).toBe(true);
  });

  it('跨模式载入：strategy 保存条件载入后恢复策略 Tab 与参数', async () => {
    (api.screenerSavedList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { id: 'd'.repeat(32), name: '策略条件', query: {
        mode: 'strategy', universe: { type: 'local', value: null }, conditions: [],
        strategy: { name: 'rsi_oversold', rsi_limit: 30 },
        factor: null, labels: null, sort: { field: 'score', direction: 'desc' }, limit: 50 },
        created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z' },
    ] });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '已保存条件' }));
    fireEvent.click(await screen.findByRole('button', { name: '载入' }));
    // 策略 Tab 激活且 rsi 参数恢复
    await waitFor(() => expect(screen.getByRole('button', { name: '策略选股' })).toBeInTheDocument());
    expect((screen.getByLabelText(/策略：/) as HTMLSelectElement).value).toBe('rsi_oversold');
    expect(screen.getByLabelText(/RSI 阈值/)).toBeInTheDocument();
  });

  it('无可用 universe 选项时禁止执行按钮', async () => {
    (api.marketSectors as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, schema_version: 1, source: 'westock-mcp', as_of: null, fetched_at: null,
      cache_status: 'unavailable', is_realtime: false, transport: 'cache_export',
      availability: {}, capability_meta: {}, data: { sectors: null }, warnings: [],
    });
    await renderScreener();
    fireEvent.change(screen.getByLabelText(/股票池：/), { target: { value: 'sector' } });
    await waitFor(() => expect(screen.getByText(/无可用选项/)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /无可选股票池/ })).toBeDisabled();
  });

  it('保存/删除/加入候选失败提示', async () => {
    (api.screenerSavedCreate as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('保存失败'));
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '保存当前条件' }));
    expect(await screen.findByText(/保存失败/)).toBeInTheDocument();

    // 删除失败（已保存条件 Tab）
    (api.screenerSavedList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { id: 'e'.repeat(32), name: 'x', query: BASE_RUN.query, created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z' },
    ] });
    (api.screenerSavedDelete as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('删除失败'));
    fireEvent.click(screen.getByRole('button', { name: '已保存条件' }));
    fireEvent.click(await screen.findByRole('button', { name: '删除' }));
    expect(await screen.findByText(/删除失败/)).toBeInTheDocument();

    // 候选移除失败
    (api.screenerCandidatesList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { symbol: '600519.SH', name: '贵州茅台', source_result_id: 'a'.repeat(32), note: '', added_at: '2026-08-01T00:00:00Z', local_history_available: true },
    ], note: '研究候选列表仅用于人工研究整理，不会生成 BigA 信号、订单或持仓，也不会同步到 Westock 自选股。' });
    (api.screenerCandidatesDelete as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('移除失败'));
    fireEvent.click(screen.getByRole('button', { name: '研究候选' }));
    fireEvent.click(await screen.findByRole('button', { name: '移除' }));
    expect(await screen.findByText(/移除失败/)).toBeInTheDocument();
  });

  it('不显示 MCP tool/路径/凭据/原始 JSON', async () => {
    (api.screenerResult as ReturnType<typeof vi.fn>).mockResolvedValue(BASE_RUN);
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    renderWithProviders(<App />, { route: '/screener/results/' + 'a'.repeat(32) });
    await screen.findByText('贵州茅台');
    const body = document.body.textContent ?? '';
    expect(body).not.toMatch(/mcp|tool_filter|strategy_select|factor_ranking|label_select/);
    expect(body).not.toMatch(/state\/dashboard|token|password/);
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.queryByText('NaN')).not.toBeInTheDocument();
    expect(screen.queryByText(/\{"|\[\{/)).not.toBeInTheDocument();
  });
});
