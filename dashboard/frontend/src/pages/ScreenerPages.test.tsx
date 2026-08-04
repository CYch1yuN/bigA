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

const RUN_RESULT = {
  ok: true, schema_version: 1, result_id: 'a'.repeat(32), mode: 'condition',
  source: 'westock-mcp', as_of: '2026-07-31', source_fetched_at: '2026-08-03T04:00:00Z',
  generated_at: '2026-08-03T12:00:00Z', cache_status: 'fresh', is_realtime: false,
  transport: 'cache_export', availability: { filter: 'fresh' },
  query: { mode: 'condition', universe: { type: 'local', value: null }, conditions: [],
    strategy: null, factor: null, labels: null, sort: { field: 'score', direction: 'desc' }, limit: 50 },
  data: { items: [
    { symbol: '600519.SH', name: '贵州茅台', score: 90, rank: 1, price: 1350, change_percent: 3.2, industry: '白酒', reason: '业绩超预期', local_history_available: true },
    { symbol: '999999.SZ', name: '无本地股', score: 30, rank: 2, price: 5, change_percent: -1, industry: '未知', reason: 'x', local_history_available: false },
  ], total: 2 },
  warnings: ['选股结果仅用于研究'],
};

async function renderScreener() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  renderWithProviders(<App />, { route: '/screener' });
  await screen.findByRole('button', { name: '条件选股' });
}

describe('选股中心 Codex 修正（Phase E）', () => {
  beforeEach(() => vi.clearAllMocks());

  it('初始页面不执行查询；六个 Tab 中文标签', async () => {
    await renderScreener();
    expect(api.screenerRun).not.toHaveBeenCalled();
    for (const label of ['条件选股', '策略选股', '标签选股', '因子排行', '已保存条件', '研究候选']) {
      expect(screen.getByRole('button', { name: label })).toBeInTheDocument();
    }
    // 无任意 universe 文本框（受控下拉）
    expect(screen.queryByPlaceholderText(/000001\.SH|BK\d|IC\d/)).not.toBeInTheDocument();
    // 无 SQL/表达式/JSON/脚本/MCP 输入框
    expect(screen.queryByPlaceholderText(/sql|expression|json|script|mcp/i)).not.toBeInTheDocument();
  });

  it('condition 模式：between 双输入发送数组；中文标签 → 英文协议值', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    // 添加一条条件并切换 between（两个数值输入）
    fireEvent.click(screen.getByRole('button', { name: '添加条件' }));
    fireEvent.change(screen.getByLabelText('条件操作符'), { target: { value: 'between' } });
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('条件值高'), { target: { value: '100' } });
    fireEvent.click(screen.getByRole('button', { name: '执行选股' }));
    await waitFor(() => expect(api.screenerRun).toHaveBeenCalled());
    const payload = (api.screenerRun as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.mode).toBe('condition');
    expect(payload.conditions[0].operator).toBe('between');
    expect(payload.conditions[0].value).toEqual([10, 100]);
    expect(payload.universe).toEqual({ type: 'local', value: null });
    // 无禁止字段
    const serialized = JSON.stringify(payload);
    expect(serialized).not.toMatch(/expression|formula|script|capability|tool|raw_params|mcp_params/);
    expect(window.location.href).toContain('/screener/results/');
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('策略模式：展示该策略允许的参数控件', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '策略选股' }));
    expect(screen.getByLabelText(/策略：/)).toBeInTheDocument();
    // ma_breakout 允许 lookback_days
    expect(screen.getByLabelText(/回看天数/)).toBeInTheDocument();
    // 切到 rsi_oversold → 显示 rsi_limit
    fireEvent.change(screen.getByLabelText(/策略：/), { target: { value: 'rsi_oversold' } });
    expect(screen.getByLabelText(/RSI 阈值/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/回看天数/)).not.toBeInTheDocument();
  });

  it('因子模式：composite 支持固定因子权重；top_n/ascending 控件', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '因子排行' }));
    fireEvent.change(screen.getByLabelText(/因子：/), { target: { value: 'composite' } });
    // 权重控件出现（价值/成长）
    expect(screen.getByLabelText(/价值：/)).toBeInTheDocument();
    expect(screen.getByLabelText(/成长：/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '执行选股' }));
    await waitFor(() => expect(api.screenerRun).toHaveBeenCalled());
    const payload = (api.screenerRun as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.factor.name).toBe('composite');
    expect(payload.factor.weights).toEqual({ value: 0.5, growth: 0.5 });
    expect(typeof payload.factor.ascending).toBe('boolean');
  });

  it('标签模式：any/all + 受控多选', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '标签选股' }));
    expect(screen.getByLabelText(/匹配：/)).toBeInTheDocument();
    expect(screen.getByLabelText(/高股息/)).toBeInTheDocument();
    expect(screen.getByLabelText(/低估值/)).toBeInTheDocument();
  });

  it('保存当前条件：保存当前编辑 query 且不执行查询；载入恢复表单', async () => {
    (api.screenerSavedCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, saved_id: 'c'.repeat(32), name: 'x' });
    (api.screenerSavedList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { id: 'b'.repeat(32), name: '载入条件', query: {
        mode: 'condition', universe: { type: 'local', value: null },
        conditions: [{ field: 'price', operator: 'gt', value: 50 }],
        strategy: null, factor: null, labels: null, sort: { field: 'score', direction: 'desc' }, limit: 50 },
        created_at: '2026-08-01T00:00:00Z', updated_at: '2026-08-01T00:00:00Z' },
    ] });
    await renderScreener();
    // 保存当前条件 → 保存当前编辑的 query
    fireEvent.click(screen.getByRole('button', { name: '保存当前条件' }));
    await waitFor(() => expect(api.screenerSavedCreate).toHaveBeenCalled());
    const savedPayload = (api.screenerSavedCreate as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(savedPayload.query.mode).toBe('condition');
    expect(api.screenerRun).not.toHaveBeenCalled();  // 保存不执行
    // 载入恢复表单：点击载入 → 切到条件 Tab 且条件行恢复
    fireEvent.click(screen.getByRole('button', { name: '已保存条件' }));
    fireEvent.click(await screen.findByRole('button', { name: '载入' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '条件选股' })).toBeInTheDocument());
  });

  it('结果页：中文查询摘要、本地/非本地链接、无交易按钮', async () => {
    (api.screenerResult as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    renderWithProviders(<App />, { route: '/screener/results/' + 'a'.repeat(32) });
    expect(await screen.findByText('贵州茅台')).toBeInTheDocument();
    // 中文查询摘要
    expect(screen.getByText(/模式：条件选股/)).toBeInTheDocument();
    expect(screen.getByText(/股票池：本地标的/)).toBeInTheDocument();
    // 本地可链接；非本地无链接 + 尚未补跑
    expect(screen.getAllByRole('link', { name: '600519.SH' }).length).toBe(1);
    expect(screen.queryByRole('link', { name: '999999.SZ' })).not.toBeInTheDocument();
    expect(screen.getByText(/999999.SZ（尚未补跑）/)).toBeInTheDocument();
    // 无交易按钮
    for (const name of [/买入/, /卖出/, /下单/, /生成信号/, /同步持仓/, /自动交易/]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    // 无原始 JSON/NaN/undefined/[object Object]
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
    expect(screen.queryByText('NaN')).not.toBeInTheDocument();
  });

  it('result_not_found 与 429 错误反馈；mutation 失败提示', async () => {
    (api.screenerResult as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('result_not_found'));
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
    renderWithProviders(<App />, { route: '/screener/results/' + 'a'.repeat(32) });
    expect(await screen.findByText(/result_not_found/)).toBeInTheDocument();
  });

  it('研究候选：固定提示 + 候选名派生（前端不提交任意 name）', async () => {
    (api.screenerCandidatesList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, items: [
      { symbol: '600519.SH', name: '贵州茅台', source_result_id: 'a'.repeat(32), note: '研究关注', added_at: '2026-08-01T00:00:00Z', local_history_available: true },
    ], note: '研究候选列表仅用于人工研究整理，不会生成 BigA 信号、订单或持仓，也不会同步到 Westock 自选股。' });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '研究候选' }));
    await waitFor(() => expect(api.screenerCandidatesList).toHaveBeenCalled());
    expect(await screen.findByText(/不会生成 BigA 信号、订单或持仓/)).toBeInTheDocument();
    expect(screen.getAllByText('贵州茅台').length).toBeGreaterThan(0);
  });
});
