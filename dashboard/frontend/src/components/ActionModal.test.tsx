import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { renderWithProviders } from '../test/render';
import App from '../App';
import { api } from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: {
      health: vi.fn(),
      login: vi.fn(),
      logout: vi.fn(),
      session: vi.fn(),
      changePassword: vi.fn(),
      safety: vi.fn(),
      dashboardSnapshot: vi.fn(),
      prepareAction: vi.fn(),
      executeAction: vi.fn(),
      jobsList: vi.fn(),
      jobGet: vi.fn(),
      jobPrepare: vi.fn(),
      jobCreate: vi.fn(),
    },
  };
});

const SAFETY = {
  live_trading: false,
  broker_connected: false,
  allowed_actions: ['verify', 'daily', 'weekly', 'rerun', 'backfill'],
  forbidden_actions: ['install', 'uninstall', 'synthetic'],
  security_statement: '仅用于研究信号与模拟账户，不连接券商，不涉及真实资金',
};

const EMPTY_SNAPSHOT = {
  ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null,
  mode: 'research_only' as const, live_trading: false, broker_connected: false,
  availability: {}, latest_run: null, artifact_run: null, gate4b: null,
  accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途',
};

function mockApiDefaults() {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600_000,
  });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue(SAFETY);
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue(EMPTY_SNAPSHOT);
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
}

describe('作业确认弹窗（JobConfirmModal）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApiDefaults();
  });

  it('操作中心按钮 → prepare → 确认 → 创建作业全流程', async () => {
    (api.jobPrepare as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, job_type: 'verify', confirm_token: 'tok-1', expires_in: 60,
    });
    (api.jobCreate as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      job: { job_id: 'abc123', job_type: 'verify', state: 'queued', created_at: '', started_at: null, finished_at: null, params: {}, daily_results: [], summary: {}, log: [], error: null },
    });

    renderWithProviders(<App />, { route: '/' });
    // 操作中心出现
    await waitFor(() => {
      expect(screen.getByTestId('operation-center')).toBeInTheDocument();
    });
    // 点击「环境检查」
    fireEvent.click(screen.getByTestId('op-verify'));
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });
    // prepare 被调用（第二个参数为参数对象，verify 无参数 → 空对象）
    expect(api.jobPrepare).toHaveBeenCalledWith('verify', {});
    // 确认弹窗展示安全声明
    expect(screen.getByText(/仅模拟账户、不涉及实盘/)).toBeInTheDocument();
    // 确认执行
    const confirm = await screen.findByRole('button', { name: '确认执行' });
    fireEvent.click(confirm);
    await waitFor(() => {
      expect(api.jobCreate).toHaveBeenCalled();
    });
  });

  it('prepare 失败显示错误', async () => {
    (api.jobPrepare as ReturnType<typeof vi.fn>).mockRejectedValue(
      Object.assign(new Error('CSRF 校验失败'), { code: 'csrf_invalid', status: 403 }),
    );
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByTestId('op-verify')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('op-verify'));
    await waitFor(() => {
      expect(screen.getByTestId('op-error')).toBeInTheDocument();
    });
    expect(screen.getByTestId('op-error').textContent).toContain('CSRF');
  });

  it('日期输入出现在每日任务弹窗', async () => {
    (api.jobPrepare as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, job_type: 'daily', confirm_token: 'tok-2', expires_in: 60,
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByTestId('op-daily')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('op-daily'));
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });
    // 日期输入框存在
    expect(screen.getByLabelText(/业务日期/)).toBeInTheDocument();
  });
});
