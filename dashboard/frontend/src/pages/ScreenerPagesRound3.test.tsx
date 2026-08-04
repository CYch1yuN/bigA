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

function saveButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: '保存当前条件' }) as HTMLButtonElement;
}

describe('选股中心 Phase E 第三轮：统一前端校验', () => {
  beforeEach(() => vi.clearAllMocks());

  it('universe 选项已加载但未选择 → 执行/保存禁用；选择后恢复', async () => {
    (api.marketSectors as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, schema_version: 1, source: 'westock-mcp', as_of: '2026-07-31', fetched_at: null,
      cache_status: 'fresh', is_realtime: false, transport: 'cache_export',
      availability: {}, capability_meta: {},
      data: { sectors: [{ code: 'BK01', name: '白酒', sector_type: 'concept' }] }, warnings: [],
    });
    await renderScreener();
    fireEvent.change(screen.getByLabelText(/股票池：/), { target: { value: 'sector' } });
    await waitFor(() => expect(screen.getByLabelText(/选择：/)).toBeInTheDocument());
    // 有选项但停在“请选择” → 禁用 + 中文原因
    expect(runButton()).toBeDisabled();
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText(/请先选择具体股票池/)).toBeInTheDocument();
    // 选择后恢复可执行
    fireEvent.change(screen.getByLabelText(/选择：/), { target: { value: 'BK01' } });
    expect(runButton()).not.toBeDisabled();
    expect(saveButton()).not.toBeDisabled();
  });

  it('enum in 全部取消 → 禁用；重新勾选恢复', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '添加条件' }));
    fireEvent.change(screen.getByLabelText('条件字段'), { target: { value: 'macd_signal' } });
    fireEvent.change(screen.getByLabelText('条件操作符'), { target: { value: 'in' } });
    // 默认金叉已勾选；全部取消 → 禁用
    fireEvent.click(screen.getByLabelText('金叉'));
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/枚举多选至少选择一个值/)).toBeInTheDocument();
    // 重新勾选 → 恢复
    fireEvent.click(screen.getByLabelText('中性'));
    expect(runButton()).not.toBeDisabled();
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('空数值 / NaN 输入 → 禁用；合法输入恢复', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '添加条件' }));
    // 清空数值
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: '' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/数值条件不能为空/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: 'abc' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/数值条件必须为有效数字/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: '12.5' } });
    expect(runButton()).not.toBeDisabled();
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('between 反向区间（lo>hi）→ 禁用；修正恢复', async () => {
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '添加条件' }));
    fireEvent.change(screen.getByLabelText('条件操作符'), { target: { value: 'between' } });
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: '100' } });
    fireEvent.change(screen.getByLabelText('条件值高'), { target: { value: '10' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/介于范围前值必须小于等于后值/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: '5' } });
    expect(runButton()).not.toBeDisabled();
  });

  it('composite 权重总和 0 → 禁用；修正恢复', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '因子排行' }));
    fireEvent.change(screen.getByLabelText(/因子：/), { target: { value: 'composite' } });
    fireEvent.change(screen.getByLabelText(/价值：/), { target: { value: '0' } });
    fireEvent.change(screen.getByLabelText(/成长：/), { target: { value: '0' } });
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/综合因子权重总和必须大于 0/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/价值：/), { target: { value: '0.5' } });
    expect(runButton()).not.toBeDisabled();
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('label 未选择 → 禁用且不静默补标签；选择后 payload 为所选', async () => {
    (api.screenerRun as ReturnType<typeof vi.fn>).mockResolvedValue(RUN_RESULT);
    const originalHref = window.location.href;
    Object.defineProperty(window, 'location', { value: { href: '' }, writable: true });
    await renderScreener();
    fireEvent.click(screen.getByRole('button', { name: '标签选股' }));
    // 默认无选择 → 禁用
    expect(runButton()).toBeDisabled();
    expect(screen.getByText(/标签选股至少选择一个标签/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/高股息/));
    expect(runButton()).not.toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '执行选股' }));
    await waitFor(() => expect(api.screenerRun).toHaveBeenCalled());
    const payload = (api.screenerRun as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.labels).toEqual({ values: ['high_dividend'], match: 'any' });
    Object.defineProperty(window, 'location', { value: { href: originalHref }, writable: true });
  });

  it('保存按钮遵循相同校验', async () => {
    (api.screenerSavedCreate as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, saved_id: 'c'.repeat(32), name: 'x' });
    await renderScreener();
    // 数值为空 → 保存按钮同样禁用
    fireEvent.click(screen.getByRole('button', { name: '添加条件' }));
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: '' } });
    expect(saveButton()).toBeDisabled();
    expect(api.screenerSavedCreate).not.toHaveBeenCalled();
    // 修正后保存恢复
    fireEvent.change(screen.getByLabelText('条件值低'), { target: { value: '10' } });
    expect(saveButton()).not.toBeDisabled();
    fireEvent.click(saveButton());
    await waitFor(() => expect(api.screenerSavedCreate).toHaveBeenCalled());
  });
});
