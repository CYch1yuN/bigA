/** 认证上下文：登录状态管理、会话检查、错误信息。 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api, ApiClientError, SafetyInfo, SessionInfo } from '../api/client';

interface AuthContextValue {
  session: SessionInfo | null;
  safety: SafetyInfo | null;
  loading: boolean;
  error: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshSafety: () => Promise<void>;
  clearError: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [safety, setSafety] = useState<SafetyInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.session();
        if (!cancelled) setSession(s);
      } catch {
        /* 未认证 */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshSafety = useCallback(async () => {
    try {
      const s = await api.safety();
      setSafety(s);
    } catch {
      /* 仅研究用途 */
    }
  }, []);

  useEffect(() => {
    if (session) {
      void refreshSafety();
    } else {
      setSafety(null);
    }
  }, [session, refreshSafety]);

  const login = useCallback(async (username: string, password: string) => {
    setError(null);
    try {
      await api.login(username, password);
      const s = await api.session();
      setSession(s);
    } catch (e) {
      const err = e as ApiClientError;
      setError(err.message || '登录失败');
      throw e;
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* 即使失败也本地登出 */
    }
    setSession(null);
    setSafety(null);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  const value = useMemo(
    () => ({ session, safety, loading, error, login, logout, refreshSafety, clearError }),
    [session, safety, loading, error, login, logout, refreshSafety, clearError],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
  return ctx;
}
