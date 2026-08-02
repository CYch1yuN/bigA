import { Navigate, Route, Routes } from 'react-router-dom';
import { useAuth } from './auth/AuthContext';
import { LoginPage } from './pages/LoginPage';
import { AppShell } from './components/AppShell';
import {
  OverviewPage,
  Gate4BPage,
  SimAccountPage,
  SignalsPage,
  DataQualityPage,
  RunHistoryPage,
  SettingsPage,
} from './pages/pages';

function ProtectedLayout() {
  const { session, loading } = useAuth();
  if (loading) {
    return <div className="app-content">加载中…</div>;
  }
  if (!session) {
    return <Navigate to="/login" replace />;
  }
  return <AppShell />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedLayout />}>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/gate4b" element={<Gate4BPage />} />
        <Route path="/sim-account" element={<SimAccountPage />} />
        <Route path="/signals" element={<SignalsPage />} />
        <Route path="/data-quality" element={<DataQualityPage />} />
        <Route path="/run-history" element={<RunHistoryPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
