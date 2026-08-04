import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
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
  ], total: 1 },
  warnings: [], cache_scope: 'q_' + '0'.repeat(64),
};

async function renderScreener() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600000 });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({ live_trading: false, broker_connected: false, allowed_actions: [], forbidden_actions: [], security_statement: '研究用途' });
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null, mode: 'research_only', live_trading: false, broker_connected: false, availability: {}, latest_run: null, artifact_run: null, gate4b: null, accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途' });
  renderWithProviders(<App />, { route: '/screener' });
  await screen.findByRole('button', { name: '条件选股' });
}

function runButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: '执行选股' }) as HTMLButtonElement;
}

describe('选股中心 Phase E 第四轮：参数范围与后端一致', () => {
  beforeEach(() => vi.clearAllMocks());

  it('limit=0 / 201 / 小数 → 禁用；1–200 整数恢复', async () => {
    await renderScreener();
    const limitInput = screen.getByLabelText(/数量：/);
    fireEvent.change(limitInput, { target: { value: '0' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/数量必须为 1–200 整数/)).toBeInTheDocument();
    fireEvent.change(limitInput, { target: { value: '201' } });
    expect(runButton()).toBeDisabled();
    fireEvent.change(limitInput, { target: { value: '10.5' } });
    expect(runButton()).toBeDisabled();
    fireEvent.change(limitInput, { target: { value: '50' } });
    expect(runButton()).not.toBeDisabled();
  });

  it('lookback_days=0 / 251 / 小数 → 禁用；20 恢复', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '策略选股' }));
    const lb = screen.getByLabelText(/回看天数/);
    fireEvent.change(lb, { target: { value: '0' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/回看天数必须为 1–250 整数/)).toBeInTheDocument();
    fireEvent.change(lb, { target: { value: '251' } });
    expect(runButton()).toBeDisabled();
    fireEvent.change(lb, { target: { value: '20.5' } });
    expect(runButton()).toBeDisabled();
    fireEvent.change(lb, { target: { value: '20' } });
    expect(runButton()).not.toBeDisabled();
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('threshold=-101 / 101 → 禁用；0 恢复', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '策略选股' }));
    fireEvent.change(screen.getByLabelText(/策略：/), { target: { value: 'trend_strength' } });
    fireEvent.change(screen.getByLabelText(/回看天数/), { target: { value: '20' } });
    const th = screen.getByLabelText(/阈值/);
    fireEvent.change(th, { target: { value: '-101' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/阈值必须为 -100～100/)).toBeInTheDocument();
    fireEvent.change(th, { target: { value: '101' } });
    expect(runButton()).toBeDisabled();
    fireEvent.change(th, { target: { value: '0' } });
    expect(runButton()).not.toBeDisabled();
  });

  it('rsi_limit=101 → 禁用；30 恢复', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '策略选股' }));
    fireEvent.change(screen.getByLabelText(/策略：/), { target: { value: 'rsi_oversold' } });
    const rsi = screen.getByLabelText(/RSI 阈值/);
    fireEvent.change(rsi, { target: { value: '101' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/RSI 阈值必须为 0～100/)).toBeInTheDocument();
    fireEvent.change(rsi, { target: { value: '30' } });
    expect(runButton()).not.toBeDisabled();
  });

  it('top_n=0 / 201 / 小数 → 禁用；10 恢复', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '因子排行' }));
    const topN = screen.getByLabelText(/数量（1-200）/);
    fireEvent.change(topN, { target: { value: '0' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/因子数量必须为 1–200 整数/)).toBeInTheDocument();
    fireEvent.change(topN, { target: { value: '201' } });
    expect(runButton()).toBeDisabled();
    fireEvent.change(topN, { target: { value: '10.5' } });
    expect(runButton()).toBeDisabled();
    fireEvent.change(topN, { target: { value: '10' } });
    expect(runButton()).not.toBeDisabled();
  });

  it('composite 一正一负 → 禁用（负权重必报错，不被正权重掩盖）；修正恢复', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '因子排行' }));
    fireEvent.change(screen.getByLabelText(/因子：/), { target: { value: 'composite' } });
    fireEvent.change(screen.getByLabelText(/价值：/), { target: { value: '0.5' } });
    fireEvent.change(screen.getByLabelText(/成长：/), { target: { value: '-0.5' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/综合因子权重不能为负/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/成长：/), { target: { value: '0.5' } });
    expect(runButton()).not.toBeDisabled();
  });

  it('composite 全零 → 禁用；修正恢复', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '因子排行' }));
    fireEvent.change(screen.getByLabelText(/因子：/), { target: { value: 'composite' } });
    fireEvent.change(screen.getByLabelText(/价值：/), { target: { value: '0' } });
    fireEvent.change(screen.getByLabelText(/成长：/), { target: { value: '0' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/综合因子权重总和必须大于 0/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/成长：/), { target: { value: '0.3' } });
    expect(runButton()).not.toBeDisabled();
  });

  it('修正为合法值后按钮恢复（保存按钮同样遵循）', async () => {
    await renderScreener();
    const save = screen.getByRole('button', { name: '保存当前条件' }) as HTMLButtonElement;
    const limitInput = screen.getByLabelText(/数量：/);
    fireEvent.change(limitInput, { target: { value: '0' } });
    expect(runButton()).toBeDisabled();
    expect(save).toBeDisabled();
    fireEvent.change(limitInput, { target: { value: '100' } });
    expect(runButton()).not.toBeDisabled();
    expect(save).not.toBeDisabled();
  });
});
