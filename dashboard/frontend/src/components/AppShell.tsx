import { useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useTheme } from '../theme/ThemeContext';
import { formatTime, useDashboardSnapshot } from '../dashboard/data';

const NAV_ITEMS = [
  { to: '/', label: '总览', icon: '◈' },
  { to: '/gate4b', label: 'Gate 4B', icon: '▣' },
  { to: '/sim-account', label: '模拟账户', icon: '◔' },
  { to: '/signals', label: '信号与订单', icon: '⇄' },
  { to: '/data-quality', label: '数据质量', icon: '◉' },
  { to: '/run-history', label: '运行记录', icon: '≡' },
  { to: '/settings', label: '系统设置', icon: '⚙' },
  { to: '/stocks', label: '股票行情', icon: 'K' },
  { to: '/market', label: '市场研究', icon: 'M' },
  { to: '/connections/westock', label: 'Westock 连接', icon: 'W' },
];

function SecurityBanner() {
  return (
    <div className="security-banner" role="status">
      <span aria-hidden="true">⚠</span>
      <span>仅用于研究信号与模拟账户，不连接券商，不涉及真实资金</span>
    </div>
  );
}

export function AppShell() {
  const { session, logout } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const snapshot = useDashboardSnapshot();

  const handleLogout = async () => {
    await logout();
  };

  return (
    <div className="app-shell">
      {sidebarOpen && (
        <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />
      )}
      <aside className={`sidebar${sidebarOpen ? ' open' : ''}`}>
        <nav className="sidebar-nav" aria-label="主导航">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
              onClick={() => setSidebarOpen(false)}
            >
              <span className="nav-icon" aria-hidden="true">
                {item.icon}
              </span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
            安全模式：研究用途
          </div>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <button
              className="sidebar-toggle btn"
              aria-label="打开导航"
              onClick={() => setSidebarOpen(true)}
              style={{ padding: '4px 8px' }}
            >
              ☰
            </button>
            <div className="topbar-brand">
              大A量化研究控制台
              <span className="brand-badge">可操作工作台</span>
            </div>
          </div>
          <div className="topbar-right">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="badge badge-neutral">数据时间：{formatTime(snapshot.data?.data_timestamp)}</span>
              <span className="badge badge-success">
                <span className="dot dot-success" aria-hidden="true" /> 系统健康
              </span>
            </div>
            <button
              className="btn"
              aria-label="切换深浅主题"
              onClick={toggleTheme}
              style={{ padding: '4px 10px' }}
            >
              {theme === 'dark' ? '☀' : '☾'}
            </button>
            <span style={{ color: 'var(--color-text-secondary)', fontSize: 14 }}>
              {session?.username ?? ''}
            </span>
            <button className="btn" onClick={handleLogout}>
              退出
            </button>
          </div>
        </header>
        <main className="app-content">
          <div style={{ marginBottom: 16 }}>
            <SecurityBanner />
          </div>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
