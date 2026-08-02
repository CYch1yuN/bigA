import { useQuery } from '@tanstack/react-query';
import { api, DashboardSnapshot } from '../api/client';
import { EmptyState } from '../components/ui';

export function useDashboardSnapshot() {
  return useQuery({
    queryKey: ['dashboard-snapshot'],
    queryFn: api.dashboardSnapshot,
    refetchInterval: 60_000,
  });
}

export function DataBoundary({
  query,
  available,
  emptyTitle,
  children,
}: {
  query: ReturnType<typeof useDashboardSnapshot>;
  available: (data: DashboardSnapshot) => boolean;
  emptyTitle: string;
  children: (data: DashboardSnapshot) => React.ReactNode;
}) {
  if (query.isLoading) return <div className="card loading-state">正在读取研究数据…</div>;
  if (query.isError) {
    return <div className="alert alert-error">数据读取失败，请确认 Dashboard 后端与认证状态。</div>;
  }
  if (!query.data || !available(query.data)) {
    return <EmptyState title={emptyTitle} hint="尚未找到对应的本地运行产物；系统不会展示虚构数据。" />;
  }
  return <>{children(query.data)}</>;
}

export function Metric({ label, value, hint, tone = 'neutral' }: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  tone?: 'neutral' | 'success' | 'warning' | 'danger';
}) {
  return (
    <div className={`metric metric-${tone}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
      {hint && <div className="metric-hint">{hint}</div>}
    </div>
  );
}

export function StatusBadge({ state }: { state?: string }) {
  const normalized = state ?? 'UNKNOWN';
  const kind = normalized === 'SUCCESS' || normalized === 'OK'
    ? 'success'
    : normalized.includes('FAIL') || normalized.includes('BLOCKED')
      ? 'danger'
      : normalized.includes('SKIPPED') ? 'warning' : 'neutral';
  return <span className={`badge badge-${kind}`}>{normalized}</span>;
}

export function formatTime(value?: string | null) {
  if (!value) return '暂无';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

export function money(value: unknown) {
  const number = Number(value ?? 0);
  return Number.isFinite(number)
    ? new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(number)
    : '—';
}
