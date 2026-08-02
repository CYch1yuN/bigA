import { FormEvent, useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { useTheme } from '../theme/ThemeContext';

export function LoginPage() {
  const { session, login, error, loading } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);

  if (!loading && session) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await login(username, password);
      navigate('/', { replace: true });
    } catch {
      /* 错误由 AuthContext 展示 */
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-card">
        <div style={{ textAlign: 'right', marginBottom: 8 }}>
          <button
            className="btn"
            aria-label="切换深浅主题"
            onClick={toggleTheme}
            style={{ padding: '4px 10px' }}
          >
            {theme === 'dark' ? '☀' : '☾'}
          </button>
        </div>
        <h1 className="login-title">大A量化研究控制台</h1>
        <p className="login-subtitle">研究信号与模拟账户 · 不涉及真实资金</p>

        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div className="field">
            <label className="field-label" htmlFor="username">
              用户名
            </label>
            <input
              id="username"
              className="input"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="password">
              密码
            </label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <button className="btn btn-primary" type="submit" disabled={submitting} style={{ width: '100%' }}>
            {submitting ? '登录中…' : '登录'}
          </button>
        </form>

        <div className="security-banner" style={{ marginTop: 20 }} role="status">
          <span aria-hidden="true">⚠</span>
          <span>仅用于研究信号与模拟账户，不连接券商，不涉及真实资金</span>
        </div>
      </div>
    </div>
  );
}
