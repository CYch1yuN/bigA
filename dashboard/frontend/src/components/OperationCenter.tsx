import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api, ApiClientError, JobRecord, JobState, JobType } from '../api/client';
import { JobConfirmModal, OPERATIONS, OperationSpec } from './JobConfirmModal';

const ACTIVE_STATES: JobState[] = ['queued', 'running'];

function jobStateKind(state: JobState): 'neutral' | 'success' | 'warning' | 'danger' {
  if (state === 'succeeded') return 'success';
  if (state === 'partial' || state === 'skipped') return 'warning';
  if (state === 'failed' || state === 'interrupted' || state === 'cancelled') return 'danger';
  return 'neutral';
}

/** 把 CLI 状态映射为中文原因（skipped 的"说明"列）。 */
function skippedReason(job: JobRecord): string {
  const cli = job.summary.cli_state ?? job.summary.skipped ?? '';
  if (cli === 'SKIPPED_DATA_UNAVAILABLE') return '数据源不可用，未生成产物';
  if (cli === 'SKIPPED_NON_TRADING_DAY') return '非交易日，任务已跳过';
  if (cli) return `任务被跳过（${cli}）`;
  return '任务被跳过';
}

/** 作业状态的中文说明（"说明"列统一入口）。 */
function jobDescription(job: JobRecord): string {
  switch (job.state) {
    case 'running':
      return '执行中…';
    case 'queued':
      return '排队中…';
    case 'failed':
      return job.error ?? '执行失败';
    case 'partial': {
      const f = job.summary.failed ?? 0;
      const s = job.summary.succeeded ?? 0;
      return `部分成功（成功 ${s} / 失败 ${f}）`;
    }
    case 'succeeded':
      return job.job_type === 'backfill' ? '区间补跑完成' : '完成';
    case 'skipped':
      return skippedReason(job);
    case 'interrupted':
      return job.error ?? '服务重启，任务已中断';
    case 'cancelled':
      return '已取消';
    default:
      return '—';
  }
}

export function OperationCenter() {
  const queryClient = useQueryClient();
  const [pendingSpec, setPendingSpec] = useState<OperationSpec | null>(null);
  const [recentJobs, setRecentJobs] = useState<JobRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const refreshJobs = useCallback(async () => {
    try {
      const resp = await api.jobsList();
      setRecentJobs(resp.jobs.slice(0, 8));
    } catch (e) {
      const err = e as ApiClientError;
      setError(err.message);
    }
  }, []);

  const refreshSnapshot = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['dashboard-snapshot'] });
  }, [queryClient]);

  // 初始加载 + 有活跃作业时轮询
  useEffect(() => {
    refreshJobs();
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
    };
  }, [refreshJobs]);

  useEffect(() => {
    const hasActive = recentJobs.some((j) => ACTIVE_STATES.includes(j.state));
    if (pollTimer.current) {
      clearInterval(pollTimer.current);
      pollTimer.current = null;
    }
    if (hasActive) {
      pollTimer.current = setInterval(refreshJobs, 3000);
    }
    // 作业完成（succeeded/partial/failed）后刷新 snapshot；skipped 无新产物不刷
    const hasFinished = recentJobs.some(
      (j) => ['succeeded', 'partial', 'failed'].includes(j.state) && j.finished_at,
    );
    if (hasFinished) refreshSnapshot();
  }, [recentJobs, refreshJobs, refreshSnapshot]);

  const handleSubmitted = useCallback((_jobId: string) => {
    setIsSubmitting(false);
    refreshJobs();
  }, [refreshJobs]);

  const handleOpen = (spec: OperationSpec) => {
    setError(null);
    setPendingSpec(spec);
  };

  return (
    <div className="card" data-testid="operation-center">
      <div className="card-title">操作中心</div>
      <p style={{ color: 'var(--color-text-secondary)', fontSize: 14, marginTop: 0, marginBottom: 12 }}>
        通过本地自动化 CLI 真实执行任务并生成本地产物；仅模拟账户，不涉及实盘。
      </p>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 16 }}>
        {OPERATIONS.map((spec) => (
          <button
            key={spec.jobType}
            className="btn btn-primary"
            data-testid={`op-${spec.jobType}`}
            onClick={() => handleOpen(spec)}
            disabled={isSubmitting}
          >
            {spec.title}
          </button>
        ))}
      </div>

      {error && (
        <div className="alert alert-error" role="alert" data-testid="op-error">
          {error}
        </div>
      )}

      {recentJobs.length > 0 && (
        <div className="card section-card">
          <div className="card-title">最近作业</div>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>作业</th>
                  <th>类型</th>
                  <th>状态</th>
                  <th>参数</th>
                  <th>进度</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {recentJobs.map((job) => (
                  <tr key={job.job_id} data-testid={`job-${job.job_id}`}>
                    <td>{job.job_id.slice(0, 8)}</td>
                    <td>{job.job_type}</td>
                    <td>
                      <span className={`badge badge-${jobStateKind(job.state)}`}>{job.state}</span>
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {job.job_type === 'backfill'
                        ? `${job.params.start_date} ~ ${job.params.end_date}`
                        : job.params.date ?? '—'}
                    </td>
                    <td style={{ fontSize: 12 }}>
                      {job.job_type === 'backfill' && job.summary.trading_days
                        ? `成 ${job.summary.succeeded ?? 0} / 败 ${job.summary.failed ?? 0} / 跳 ${job.summary.skipped_days ?? 0}（共 ${job.summary.trading_days}）`
                        : job.summary.duration_ms
                          ? `${Math.round(job.summary.duration_ms / 1000)}s`
                          : '—'}
                    </td>
                    <td style={{ fontSize: 12 }}>{jobDescription(job)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {pendingSpec && (
        <JobConfirmModal
          spec={pendingSpec}
          onClose={() => setPendingSpec(null)}
          onSubmitted={handleSubmitted}
          onError={(m) => setError(m)}
        />
      )}
    </div>
  );
}

export type { JobType };
