import { useAuth } from '../auth/AuthContext';
import { PageHeader } from '../components/ui';
import { OperationCenter } from '../components/OperationCenter';
import { PredictionSummaryCard } from '../components/PredictionSummaryCard';
import { DataBoundary, Metric, StatusBadge, formatTime, useDashboardSnapshot } from '../dashboard/data';

export function OverviewPage() {
  const { safety } = useAuth();
  const query = useDashboardSnapshot();
  return (
    <div>
      <PageHeader title="总览" description="大A量化研究控制台 · 研究用途" />
      <div style={{ display: 'grid', gap: 16, gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))' }}>
        <div className="card">
          <div className="card-title">实时交易</div>
          <div className="badge badge-neutral">未启用</div>
          <p style={{ color: 'var(--color-text-secondary)', fontSize: 14, marginTop: 12, marginBottom: 0 }}>
            仅用于研究信号与模拟账户，不连接券商，不涉及真实资金
          </p>
        </div>
        <div className="card">
          <div className="card-title">券商连接</div>
          <div className="badge badge-neutral">未连接</div>
          <p style={{ color: 'var(--color-text-secondary)', fontSize: 14, marginTop: 12, marginBottom: 0 }}>
            当前仅研究模式，不涉及任何实盘操作
          </p>
        </div>
        <div className="card">
          <div className="card-title">安全边界</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <span className="badge badge-success">
              <span className="dot dot-success" aria-hidden="true" /> 仅限研究
            </span>
            <span className="badge badge-warning">作业类型：{safety?.allowed_actions.length ?? 0} 项</span>
          </div>
        </div>
      </div>

      <div style={{ marginTop: 16 }}>
        <OperationCenter />
      </div>

      <div style={{ marginTop: 16 }}>
        <PredictionSummaryCard />
      </div>

      <div style={{ marginTop: 16 }}>
        <DataBoundary
          query={query}
          available={(data) => Boolean(data.latest_run || data.artifact_run)}
          emptyTitle="数据概览"
        >
          {(data) => {
            const run = data.latest_run ?? data.artifact_run;
            const quality = data.quality?.summary ?? {};
            const accounts = data.accounts?.accounts ?? [];
            return (
              <>
                <div className="metric-grid">
                  <Metric label="最近任务" value={<StatusBadge state={run?.state} />} hint={run?.as_of_date ?? data.artifact_date ?? '暂无日期'} />
                  <Metric label="模拟账户" value={accounts.length} hint="稳健轨与激进轨" tone="success" />
                  <Metric label="质量告警" value={Number(quality.critical ?? 0) + Number(quality.warning ?? 0)} hint={`严重 ${quality.critical ?? 0} · 警告 ${quality.warning ?? 0}`} tone={quality.critical ? 'danger' : quality.warning ? 'warning' : 'success'} />
                  <Metric label="数据更新时间" value={data.artifact_date ?? '暂无'} hint={formatTime(data.data_timestamp)} />
                </div>
                <div className="card section-card">
                  <div className="card-title">最近运行说明</div>
                  <p className="body-copy">{run?.message ?? '已有研究产物，但没有运行说明。'}</p>
                </div>
              </>
            );
          }}
        </DataBoundary>
      </div>
    </div>
  );
}
