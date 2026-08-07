import { useQuery } from '@tanstack/react-query';
import { api, PredictionSummaryResponse } from '../api/client';

// ---------------------------------------------------------------------- //
// 格式化：永不输出 NaN / Infinity / undefined / [object Object]
// 比例字段（net_return / max_drawdown / benchmark_return）一律复用 ×100 百分比格式
// ---------------------------------------------------------------------- //

function fmtPct(value: unknown, digits = 1): string {
  if (value == null) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtNum(value: unknown, digits = 2): string {
  if (value == null) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toFixed(digits);
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', { hour12: false });
}

// ---------------------------------------------------------------------- //
// 状态徽标
// ---------------------------------------------------------------------- //

function GateBadge({ gate }: { gate: string }) {
  if (gate === 'passed') {
    return <span className="badge badge-success">已通过</span>;
  }
  if (gate === 'insufficient_data') {
    return <span className="badge badge-warning">样本不足</span>;
  }
  return <span className="badge badge-danger">未达到门槛</span>;
}

// ---------------------------------------------------------------------- //
// 卡片
// ---------------------------------------------------------------------- //

export function PredictionSummaryCard() {
  const query = useQuery({
    queryKey: ['prediction-summary'],
    queryFn: () => api.predictionSummary(),
    staleTime: 60_000,
  });

  if (query.isError) {
    return (
      <div className="card section-card">
        <div className="card-title compact">预测有效性</div>
        <span className="badge badge-neutral">暂无评估结果</span>
        <p className="body-copy muted" style={{ marginBottom: 0 }}>
          评估摘要不可用；不影响本页其他卡片。
        </p>
      </div>
    );
  }

  const resp: PredictionSummaryResponse | undefined = query.data;
  const data = resp?.data ?? null;
  const unavailable = !data || resp?.availability === 'unavailable';
  const stale = resp?.availability === 'stale';
  const sampleInsufficient = Boolean(
    unavailable && resp?.warnings?.some((w) => w.includes('样本数不足')),
  );

  return (
    <div className="card section-card">
      <div className="card-title compact">预测有效性</div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {unavailable ? (
          <span className={`badge ${sampleInsufficient ? 'badge-warning' : 'badge-neutral'}`}>
            {sampleInsufficient ? '样本不足' : '暂无评估结果'}
          </span>
        ) : (
          <GateBadge gate={data.gate_status} />
        )}
        {stale && <span className="badge badge-warning">评估结果已过期</span>}
        <span className="badge badge-neutral">
          {unavailable ? '非实时 · 本地评估' : '非实时 · 样本外评估'}
        </span>
      </div>

      {unavailable ? (
        <p className="body-copy muted" style={{ marginBottom: 0, marginTop: 12 }}>
          {resp?.warnings?.[0] ?? '暂无经过严格样本外验证的预测准确率'}
        </p>
      ) : (
        <>
          <p className="body-copy" style={{ marginTop: 12, marginBottom: 12 }}>
            任务定义：{data.task_name}
            <span className="muted" style={{ marginLeft: 8 }}>
              （{data.horizon_days} 个交易日 · 目标收益 {fmtPct(data.target_return, 0)}）
            </span>
          </p>

          <div className="metric-grid">
            <div className="metric">
              <div className="metric-label">样本外准确率</div>
              <div className="metric-value">{fmtPct(data.accuracy)}</div>
              <div className="metric-hint">样本 {data.sample_count} 个</div>
            </div>
            <div className="metric">
              <div className="metric-label">Precision</div>
              <div className="metric-value">{fmtPct(data.precision)}</div>
              <div className="metric-hint">精确率</div>
            </div>
            <div className="metric">
              <div className="metric-label">Recall</div>
              <div className="metric-value">{fmtPct(data.recall)}</div>
              <div className="metric-hint">召回率</div>
            </div>
            <div className="metric">
              <div className="metric-label">测试区间</div>
              <div className="metric-value" style={{ fontSize: 14 }}>{data.test_start} ~ {data.test_end}</div>
              <div className="metric-hint">样本外区间</div>
            </div>
          </div>

          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginTop: 10 }}>
            {data.net_return != null && (
              <span className="body-copy muted">扣费后收益：{fmtPct(data.net_return, 2)}</span>
            )}
            {data.max_drawdown != null && (
              <span className="body-copy muted">最大回撤：{fmtPct(data.max_drawdown, 2)}</span>
            )}
            {data.benchmark_return != null && (
              <span className="body-copy muted">基准收益：{fmtPct(data.benchmark_return, 2)}</span>
            )}
            {data.sharpe != null && (
              <span className="body-copy muted">Sharpe：{fmtNum(data.sharpe)}</span>
            )}
            {data.auc != null && (
              <span className="body-copy muted">AUC：{fmtNum(data.auc, 3)}</span>
            )}
          </div>

          {data.gate_reasons.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <p className="body-copy muted" style={{ marginBottom: 4 }}>未通过门槛：</p>
              <ul style={{ margin: 0, paddingLeft: 18 }} className="body-copy muted">
                {data.gate_reasons.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
              <p className="body-copy muted" style={{ marginBottom: 0, marginTop: 4 }}>
                门槛版本：{data.gate_version}
              </p>
            </div>
          )}

          <p className="body-copy muted" style={{ marginTop: 10, marginBottom: 0 }}>
            评估时间：{fmtDate(resp?.as_of)} · 数据日期：{data.test_end}
          </p>
          {stale && (
            <p className="body-copy muted" style={{ marginTop: 6, marginBottom: 0 }}>
              ⚠ 评估结果已过期（超过 7 天未更新），数值仅供历史参考。
            </p>
          )}
        </>
      )}
    </div>
  );
}
