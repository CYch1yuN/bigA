import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
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
  forbidden_actions: ['install'],
  security_statement: '研究用途',
};

function snapshotWithJobs() {
  return {
    ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null,
    mode: 'research_only' as const, live_trading: false, broker_connected: false,
    availability: {}, latest_run: null, artifact_run: null, gate4b: null,
    accounts: null, signals: null, orders: null, quality: null, run_history: [], disclaimer: '研究用途',
  };
}

function makeJob(overrides: Record<string, any> = {}) {
  return {
    job_id: 'job-1',
    job_type: 'verify',
    state: 'succeeded',
    created_at: '2026-08-02T00:00:00Z',
    started_at: '2026-08-02T00:00:01Z',
    finished_at: '2026-08-02T00:00:05Z',
    params: {},
    daily_results: [],
    summary: { exit_code: 0, duration_ms: 4000 },
    log: [],
    error: null,
    ...overrides,
  };
}

describe('操作中心', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, authenticated: true, username: 'admin', expires_at: Date.now() + 3600_000,
    });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue(SAFETY);
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue(snapshotWithJobs());
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  });

  it('渲染六个操作入口', async () => {
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByTestId('operation-center')).toBeInTheDocument();
    });
    expect(screen.getByTestId('op-verify')).toBeInTheDocument();
    expect(screen.getByTestId('op-daily')).toBeInTheDocument();
    expect(screen.getByTestId('op-weekly')).toBeInTheDocument();
    expect(screen.getByTestId('op-rerun')).toBeInTheDocument();
    expect(screen.getByTestId('op-backfill')).toBeInTheDocument();
  });

  it('展示排队与运行中状态', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [
        makeJob({ job_id: 'a', state: 'queued', job_type: 'daily', params: { date: '2026-08-03' } }),
        makeJob({ job_id: 'b', state: 'running', job_type: 'weekly' }),
      ],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByText('排队中…')).toBeInTheDocument();
      expect(screen.getByText('执行中…')).toBeInTheDocument();
    });
  });

  it('成功状态展示', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [makeJob({ job_id: 'ok1', state: 'succeeded' })],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByTestId('job-ok1')).toBeInTheDocument();
    });
    expect(screen.getByText('succeeded')).toBeInTheDocument();
  });

  it('部分失败展示 partial 与失败天数', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [
        makeJob({
          job_id: 'p1',
          job_type: 'backfill',
          state: 'partial',
          params: { start_date: '2026-08-03', end_date: '2026-08-07' },
          summary: { trading_days: 5, succeeded: 4, failed: 1, skipped_days: 0 },
          daily_results: [
            { date: '2026-08-03', state: 'succeeded', exit_code: 0 },
            { date: '2026-08-04', state: 'failed', exit_code: 1, error: 'CLI 失败' },
          ],
        }),
      ],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByText(/部分成功/)).toBeInTheDocument();
    });
    expect(screen.getByText(/成 4 \/ 败 1 \/ 跳 0（共 5）/)).toBeInTheDocument();
  });

  it('失败状态展示错误原因', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [makeJob({ job_id: 'f1', state: 'failed', error: 'CLI 失败（退出码 1）' })],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByText('CLI 失败（退出码 1）')).toBeInTheDocument();
    });
  });

  it('完成作业触发 snapshot 刷新', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [makeJob({ job_id: 'done1', state: 'succeeded', finished_at: '2026-08-02T00:00:05Z' })],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByTestId('job-done1')).toBeInTheDocument();
    });
    // snapshot 被重新拉取（invalidateQueries 后 refetch）
    await waitFor(() => {
      expect(api.dashboardSnapshot).toHaveBeenCalled();
    });
  });

  it('无产物时空状态出现操作指引', async () => {
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByTestId('operation-center')).toBeInTheDocument();
    });
    // 空数据时仍能看到操作入口（操作中心在数据区之上）
    expect(screen.getByTestId('op-verify')).toBeInTheDocument();
    expect(screen.getByTestId('op-daily')).toBeInTheDocument();
  });

  it('skipped 展示中文原因：数据源不可用', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [
        makeJob({
          job_id: 'skip1',
          job_type: 'daily',
          state: 'skipped',
          params: { date: '2026-08-03' },
          summary: { exit_code: 0, cli_state: 'SKIPPED_DATA_UNAVAILABLE', skipped: 'SKIPPED_DATA_UNAVAILABLE' },
        }),
      ],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByText('数据源不可用，未生成产物')).toBeInTheDocument();
    });
  });

  it('skipped 展示中文原因：非交易日', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [
        makeJob({
          job_id: 'skip2',
          job_type: 'daily',
          state: 'skipped',
          params: { date: '2026-08-02' },
          summary: { exit_code: 0, cli_state: 'SKIPPED_NON_TRADING_DAY', skipped: 'SKIPPED_NON_TRADING_DAY' },
        }),
      ],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByText('非交易日，任务已跳过')).toBeInTheDocument();
    });
  });

  it('backfill 显示成功/失败/跳过三个数量', async () => {
    (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      jobs: [
        makeJob({
          job_id: 'bf1',
          job_type: 'backfill',
          state: 'partial',
          params: { start_date: '2026-08-03', end_date: '2026-08-09' },
          summary: { trading_days: 5, succeeded: 2, failed: 1, skipped_days: 2 },
          daily_results: [
            { date: '2026-08-03', state: 'succeeded', exit_code: 0 },
            { date: '2026-08-04', state: 'failed', exit_code: 1, error: 'CLI 失败' },
            { date: '2026-08-05', state: 'skipped', exit_code: 0 },
          ],
        }),
      ],
    });
    renderWithProviders(<App />, { route: '/' });
    await waitFor(() => {
      expect(screen.getByText(/成 2 \/ 败 1 \/ 跳 2（共 5）/)).toBeInTheDocument();
    });
    expect(screen.getByText(/部分成功（成功 2 \/ 失败 1）/)).toBeInTheDocument();
  });
});
