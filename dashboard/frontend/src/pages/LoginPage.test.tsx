import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { renderWithProviders } from '../test/render';
import App from '../App';
import { api } from '../api/client';

// 所有页面不得出现真实交易文案
const FORBIDDEN_WORDS = ['买入', '卖出', '下单', '充值', '券商连接', '实盘开关', '一年十倍'];

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
    },
  };
});

describe('登录流程', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      authenticated: true,
      username: 'admin',
      expires_at: Date.now() + 3600_000,
    });
    (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true, schema_version: 1, generated_at: '', data_timestamp: null, artifact_date: null,
      mode: 'research_only', live_trading: false, broker_connected: false,
      availability: {}, latest_run: null, artifact_run: null, gate4b: null,
      accounts: null, signals: null, orders: null, quality: null, run_history: [],
      disclaimer: '研究用途',
    });
  });

  it('未认证时跳转到登录页', async () => {
    (api.session as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('401'));
    renderWithProviders(<App />, { route: '/gate4b' });
    await waitFor(() => {
      expect(screen.getByText('大A量化研究控制台')).toBeInTheDocument();
    });
    // 登录表单出现
    expect(screen.getByLabelText('用户名')).toBeInTheDocument();
    expect(screen.getByLabelText('密码')).toBeInTheDocument();
  });

  it('登录成功后进入应用外壳', async () => {
    (api.session as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('401'));
    renderWithProviders(<App />, { route: '/login' });
    await waitFor(() => {
      expect(screen.getByLabelText('用户名')).toBeInTheDocument();
    });
    (api.login as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, username: 'admin' });
    (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue({
      live_trading: false,
      broker_connected: false,
      allowed_actions: ['verify'],
      forbidden_actions: ['install'],
      security_statement: '仅用于研究信号与模拟账户，不连接券商，不涉及真实资金',
    });
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret123' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() => {
      expect(screen.getAllByText('总览').length).toBeGreaterThan(0);
    });
    expect(api.login).toHaveBeenCalledWith('admin', 'secret123');
  });

  it('登录失败显示错误', async () => {
    (api.session as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('401'));
    renderWithProviders(<App />, { route: '/login' });
    await waitFor(() => {
      expect(screen.getByLabelText('用户名')).toBeInTheDocument();
    });
    (api.login as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('用户名或密码错误'));
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'wrong' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
    });
  });

  it('登录页不含真实交易文案', () => {
    const { container } = renderWithProviders(<App />, { route: '/login' });
    const text = container.textContent ?? '';
    FORBIDDEN_WORDS.forEach((w) => expect(text).not.toContain(w));
  });
});
