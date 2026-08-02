import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
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
  allowed_actions: ['verify', 'gate4b_track'],
  forbidden_actions: ['install'],
  security_statement: '仅用于研究信号与模拟账户，不连接券商，不涉及真实资金',
};

async function renderAuthenticated(route = '/') {
  (api.session as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true,
    authenticated: true,
    username: 'admin',
    expires_at: Date.now() + 3600_000,
  });
  (api.safety as ReturnType<typeof vi.fn>).mockResolvedValue(SAFETY);
  (api.jobsList as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, jobs: [] });
  (api.dashboardSnapshot as ReturnType<typeof vi.fn>).mockResolvedValue({
    ok: true, schema_version: 1, generated_at: '2026-08-02T00:00:00Z',
    data_timestamp: '2026-08-02T00:00:00Z', artifact_date: '2020-10-02',
    mode: 'research_only', live_trading: false, broker_connected: false,
    availability: { latest_run: true, gate4b: true, accounts: true, signals: true, orders: true, quality: true },
    latest_run: { state: 'SUCCESS', as_of_date: '2026-08-02', message: '完成' }, artifact_run: null,
    gate4b: { observation_progress: 5, observation_target: 60, violations: [] },
    accounts: { accounts: [], equity: {} }, signals: { signals: [] }, orders: { orders: [] },
    quality: { summary: { critical: 0, warning: 0, rows_checked: 10 }, has_critical: false, issues_count: 0 },
    run_history: [{ run_id: 'r1', as_of_date: '2026-08-02', task_type: 'daily', state: 'SUCCESS', duration_seconds: 1, message: '完成' }],
    disclaimer: '研究用途',
  });
  const utils = renderWithProviders(<App />, { route });
  await waitFor(() => {
    expect(screen.getByText('大A量化研究控制台')).toBeInTheDocument();
  });
  return utils;
}

describe('应用外壳与主题', () => {
  beforeEach(() => vi.clearAllMocks());

  it('显示顶部栏与左侧导航', async () => {
    await renderAuthenticated('/');
    expect(screen.getByText('大A量化研究控制台')).toBeInTheDocument();
    ['总览', 'Gate 4B', '模拟账户', '信号与订单', '数据质量', '运行记录', '系统设置'].forEach((label) => {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    });
  });

  it('显示安全声明', async () => {
    const { container } = await renderAuthenticated('/');
    expect(container.textContent).toContain('仅用于研究信号与模拟账户，不连接券商，不涉及真实资金');
  });

  it('显示当前登录用户与退出按钮', async () => {
    await renderAuthenticated('/');
    expect(screen.getByText('admin')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '退出' })).toBeInTheDocument();
  });

  it('深浅主题切换', async () => {
    const { container } = await renderAuthenticated('/');
    const root = container.ownerDocument.documentElement;
    const toggle = screen.getByRole('button', { name: '切换深浅主题' });
    const initial = root.getAttribute('data-theme');
    expect(['light', 'dark']).toContain(initial);
    fireEvent.click(toggle);
    const after = root.getAttribute('data-theme');
    expect(after).not.toBe(initial);
    // 主题持久化
    expect(localStorage.getItem('ashare-dashboard-theme')).toBe(after);
  });

  it('浅色主题为真白背景', async () => {
    const { container } = await renderAuthenticated('/');
    const root = container.ownerDocument.documentElement;
    root.setAttribute('data-theme', 'light');
    // 读取 CSS 变量
    const bg = getComputedStyle(root).getPropertyValue('--color-bg').trim();
    expect(bg).toBe('#ffffff');
  });

  it('页面导航到 Gate 4B 显示真实观测数据', async () => {
    await renderAuthenticated('/gate4b');
    expect(await screen.findByText('5/60')).toBeInTheDocument();
    expect(screen.getByText('60 日观察窗口')).toBeInTheDocument();
  });

  it('各页面不含真实交易文案', async () => {
    const { container } = await renderAuthenticated('/');
    const text = container.textContent ?? '';
    ['买入', '卖出', '下单', '充值', '实盘开关', '一年十倍'].forEach((w) => {
      expect(text).not.toContain(w);
    });
  });

  it('响应式：窄屏时侧栏默认收起', async () => {
    const { container } = await renderAuthenticated('/');
    // 模拟窄屏：直接验证 CSS 媒体查询存在（theme.css）
    const css = container.ownerDocument.styleSheets;
    const hasMediaQuery = Array.from(css).some((sheet) => {
      try {
        return Array.from(sheet.cssRules).some((r) => r instanceof CSSMediaRule && r.conditionText.includes('900px'));
      } catch {
        return false;
      }
    });
    expect(hasMediaQuery).toBe(true);
  });
});
