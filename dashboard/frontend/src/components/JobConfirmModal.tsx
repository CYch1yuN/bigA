import { useEffect, useState } from 'react';
import { api, ApiClientError, JobType } from '../api/client';

export interface OperationSpec {
  jobType: JobType;
  title: string;
  description: string;
  /** 需要额外输入：date / range */
  input: 'none' | 'date' | 'range';
  defaultDate?: string;
}

export const OPERATIONS: OperationSpec[] = [
  {
    jobType: 'verify',
    title: '环境检查',
    description: '校验安全边界与配置可行性（只读，不产生业务产物）',
    input: 'none',
  },
  {
    jobType: 'daily',
    title: '运行今日任务',
    description: '执行今日每日自动化管线（真实 CLI，仅模拟账户）',
    input: 'date',
  },
  {
    jobType: 'weekly',
    title: '每周运行',
    description: '执行每周汇总（真实 CLI，仅模拟账户）',
    input: 'none',
  },
  {
    jobType: 'rerun',
    title: '最近失败任务重跑',
    description: '重跑最近失败的每日任务（真实 CLI，仅模拟账户）',
    input: 'none',
  },
  {
    jobType: 'backfill',
    title: '日期区间补跑',
    description: '按交易日历逐日补跑区间（单日失败继续，仅模拟账户）',
    input: 'range',
  },
];

function today(): string {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

interface JobConfirmModalProps {
  spec: OperationSpec;
  onClose: () => void;
  onSubmitted: (jobId: string) => void;
  onError: (message: string) => void;
}

/**
 * 作业确认弹窗：prepare 获取一次性确认令牌 → 展示动作/日期/用途 → 确认创建。
 * 明确展示“仅模拟账户、不涉及实盘”。
 */
export function JobConfirmModal({ spec, onClose, onSubmitted, onError }: JobConfirmModalProps) {
  const [phase, setPhase] = useState<'preparing' | 'confirming' | 'submitting'>('preparing');
  const [token, setToken] = useState<string | null>(null);
  const [date, setDate] = useState(today());
  const [startDate, setStartDate] = useState(today());
  const [endDate, setEndDate] = useState(today());

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // 确认令牌绑定当前参数（日期/区间），防止确认与执行参数不一致
        const prepareParams =
          spec.input === 'range'
            ? { start_date: startDate, end_date: endDate }
            : spec.input === 'date'
              ? { date }
              : {};
        const resp = await api.jobPrepare(spec.jobType, prepareParams);
        if (!cancelled) {
          setToken(resp.confirm_token);
          setPhase('confirming');
        }
      } catch (e) {
        const err = e as ApiClientError;
        if (!cancelled) {
          onError(err.message);
          onClose();
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.jobType]);

  const handleConfirm = async () => {
    if (!token || phase !== 'confirming') return;
    setPhase('submitting');
    try {
      const params =
        spec.input === 'range'
          ? { start_date: startDate, end_date: endDate }
          : spec.input === 'date'
            ? { date }
            : {};
      const resp = await api.jobCreate(spec.jobType, params, token);
      onSubmitted(resp.job.job_id);
      onClose();
    } catch (e) {
      const err = e as ApiClientError;
      setPhase('confirming');
      onError(err.message);
    }
  };

  const description =
    spec.input === 'range'
      ? `${spec.title}：${startDate} ~ ${endDate}`
      : spec.input === 'date'
        ? `${spec.title}：${date}`
        : spec.title;

  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={`确认执行 ${spec.title}`}>
      <div className="modal">
        <div className="modal-title">确认执行操作</div>
        <div className="modal-body">
          <p style={{ marginTop: 0 }}>
            将执行 <strong>{description}</strong>
          </p>
          <p style={{ color: 'var(--color-text-secondary)', fontSize: 14 }}>
            用途：{spec.description}
          </p>
          {spec.input === 'date' && (
            <label style={{ display: 'block', marginBottom: 12 }}>
              业务日期（YYYY-MM-DD）
              <input
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
                style={{ marginLeft: 8 }}
              />
            </label>
          )}
          {spec.input === 'range' && (
            <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
              <label>
                开始
                <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
              </label>
              <label>
                结束
                <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
              </label>
            </div>
          )}
          <div className="badge badge-warning" style={{ marginTop: 8 }}>
            仅模拟账户、不涉及实盘；真实调用本地自动化 CLI
          </div>
          {phase === 'preparing' && <p>正在获取一次性确认令牌…</p>}
          {phase === 'submitting' && <p>正在提交作业…</p>}
        </div>
        <div className="modal-actions">
          <button className="btn" onClick={onClose} disabled={phase === 'submitting'}>
            取消
          </button>
          <button
            className="btn btn-primary"
            onClick={handleConfirm}
            disabled={phase !== 'confirming'}
          >
            确认执行
          </button>
        </div>
      </div>
    </div>
  );
}
