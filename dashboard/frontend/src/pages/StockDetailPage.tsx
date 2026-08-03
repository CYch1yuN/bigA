import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api, StockRange } from '../api/client';
import { EChartsKLine, mapTradeMarkers } from '../components/EChartsKLine';
import { EChartsMinuteChart } from '../components/EChartsMinuteChart';
import { PageHeader } from '../components/ui';

const RANGES: StockRange[] = ['1m', '3m', '6m', '1y', '3y', 'all'];
const RANGE_LABEL: Record<StockRange, string> = {
  '1m': '1月', '3m': '3月', '6m': '6月', '1y': '1年', '3y': '3年', all: '全部',
};

export function StockDetailPage() {
  const { symbol = '' } = useParams();
  const [adjustment, setAdjustment] = useState<'raw' | 'qfq'>('qfq');
  const [range, setRange] = useState<StockRange>('all');

  const history = useQuery({
    queryKey: ['stock-history', symbol, adjustment, range],
    queryFn: () => api.stocksHistory(symbol, { adjustment, range }),
    enabled: !!symbol,
  });
  const snapshot = useQuery({
    queryKey: ['stock-snapshot', symbol],
    queryFn: () => api.stocksSnapshot(symbol),
    enabled: !!symbol,
  });
  const minute = useQuery({
    queryKey: ['stock-minute', symbol],
    queryFn: () => api.stocksMinute(symbol),
    enabled: !!symbol,
  });
  const research = useQuery({
    queryKey: ['stock-research', symbol],
    queryFn: () => api.stocksResearch(symbol),
    enabled: !!symbol,
  });

  const bars = useMemo(() => history.data?.data.rows ?? [], [history.data]);
  const markers = useMemo(
    () => mapTradeMarkers(bars, research.data?.data.orders ?? []),
    [bars, research.data],
  );

  const local = snapshot.data?.data.local;
  const westockQuote = snapshot.data?.data.westock_quote;
  const minuteAvailable = minute.data?.availability.westock_minute === true;
  const minuteRows = minute.data?.data && 'rows' in minute.data.data
    ? (minute.data.data as { rows: { time: string; price: number; volume: number | null }[] }).rows
    : [];
  const historyUnavailable = history.data && history.data.data.rows.length === 0;

  const quoteTone = westockQuote
    ? Number(westockQuote.change_percent ?? 0) >= 0 ? 'var(--color-danger)' : 'var(--color-success)'
    : 'var(--color-text-muted)';

  return (
    <div>
      <PageHeader title={symbol} description="本地 curated 历史行情 · Westock 仅作缓存旁路" />

      {history.isError && <div className="alert alert-error">无法读取该标的行情；本地研究主链不受影响。</div>}

      {historyUnavailable && (
        <div className="alert alert-warning">
          {history.data?.availability.curated === false
            ? '尚未补跑历史行情：本地 curated 没有该标的的 K 线数据，不显示虚构行情。'
            : history.data?.message ?? '该区间无数据。'}
        </div>
      )}

      {/* 快照指标：本地行情 + Westock 报价缓存（受控字段，明确 fresh/stale） */}
      <div className="metric-grid">
        <div className="metric">
          <div className="metric-label">最新收盘（本地）</div>
          <div className="metric-value">{local?.close ?? '—'}</div>
          <div className="metric-hint">交易日 {local?.date ?? '无'}</div>
        </div>
        <div className="metric">
          <div className="metric-label">涨跌 / 涨跌幅</div>
          <div className="metric-value" style={{ color: Number(local?.change_percent ?? 0) >= 0 ? 'var(--color-danger)' : 'var(--color-success)' }}>
            {local?.change != null ? `${local.change} (${local.change_percent}%)` : '—'}
          </div>
          <div className="metric-hint">本地历史计算</div>
        </div>
        <div className="metric">
          <div className="metric-label">成交量 / 成交额</div>
          <div className="metric-value">{local?.volume != null ? local.volume.toLocaleString() : '—'}</div>
          <div className="metric-hint">{local?.amount != null ? `¥${(local.amount / 1e8).toFixed(2)} 亿` : '—'}</div>
        </div>
        <div className="metric">
          <div className="metric-label">Westock 报价</div>
          <div className="metric-value" style={{ color: quoteTone }}>
            {westockQuote ? westockQuote.price.toFixed(2) : '—'}
          </div>
          <div className="metric-hint">
            {westockQuote
              ? `${westockQuote.status === 'fresh' ? '新鲜' : '已过期'} · ${westockQuote.change_percent != null ? `${westockQuote.change_percent >= 0 ? '+' : ''}${westockQuote.change_percent}%` : '涨跌未知'}${westockQuote.time ? ` · ${westockQuote.time}` : ''} · 缓存导出`
              : '无缓存 · 非实时'}
          </div>
        </div>
      </div>

      {/* 图表控制 */}
      {!historyUnavailable && history.data && history.data.data.rows.length > 0 && (
        <div className="card section-card">
          <div className="stock-chart-controls">
            <div className="btn-group">
              {(['raw', 'qfq'] as const).map((adj) => (
                <button key={adj} className={`btn btn-sm${adjustment === adj ? ' btn-active' : ''}`}
                  onClick={() => setAdjustment(adj)}>
                  {adj === 'qfq' ? '前复权' : '不复权'}
                </button>
              ))}
            </div>
            <div className="btn-group">
              {RANGES.map((r) => (
                <button key={r} className={`btn btn-sm${range === r ? ' btn-active' : ''}`}
                  onClick={() => setRange(r)}>
                  {RANGE_LABEL[r]}
                </button>
              ))}
            </div>
          </div>
          <EChartsKLine bars={bars} adjustment={adjustment} markers={markers} />
          <p className="body-copy muted">
            qfq 仅来自本地 curated 真实复权字段；Westock 复权不进入历史图表。
            {markers.length > 0
              ? `已叠加 ${markers.length} 个真实成交/信号点（仅存在于对应交易日的标记）。`
              : '无匹配交易日的买卖点可叠加。'}
          </p>
        </div>
      )}

      {/* 分时：仅缓存存在且有合法数据时展示折线图 */}
      {minuteAvailable && minuteRows.length > 0 && (
        <div className="card section-card">
          <div className="card-title compact">分时（Westock 缓存导出，非实时）</div>
          <EChartsMinuteChart rows={minuteRows} />
          <p className="body-copy muted">
            分时来自 Westock 缓存，不宣称实时；fetched_at：{minute.data?.fetched_at ?? '—'}
            {minute.data?.cache_status === 'stale' ? '（缓存已过期，仅作展示）' : ''}
          </p>
        </div>
      )}
      {(!minuteAvailable || (minuteAvailable && minuteRows.length === 0)) && minute.data && (
        <div className="card section-card">
          <div className="card-title compact">分时</div>
          <p className="body-copy muted">Westock 分时缓存不存在、已过期或数据无法标准化；不影响本页其他区域。</p>
        </div>
      )}

      {/* 信号 / 订单 / 持仓 */}
      <div className="card section-card">
        <div className="card-title compact">BigA 信号 · 模拟订单 · 持仓（只读）</div>
        <div className="stock-groups">
          <div>
            <div className="muted">信号（{research.data?.data.signals.length ?? 0}）</div>
            {research.data?.data.signals.length ? (
              <ul className="stock-list">
                {research.data.data.signals.map((s, i) => (
                  <li key={i}>{s.signal_date} {s.side} {s.quantity ?? ''} 股 — {s.reason}</li>
                ))}
              </ul>
            ) : <p className="body-copy muted">无信号记录。</p>}
          </div>
          <div>
            <div className="muted">模拟订单（{research.data?.data.orders.length ?? 0}）</div>
            {research.data?.data.orders.length ? (
              <ul className="stock-list">
                {research.data.data.orders.map((o, i) => (
                  <li key={i}>{o.fill_date ?? o.signal_date} {o.side} {o.quantity ?? ''} 股 {o.status}
                    {o.fill_price ? ` @ ${o.fill_price}` : ''}</li>
                ))}
              </ul>
            ) : <p className="body-copy muted">无订单记录。</p>}
          </div>
          <div>
            <div className="muted">当前持仓（{research.data?.data.positions.length ?? 0}）</div>
            {research.data?.data.positions.length ? (
              <ul className="stock-list">
                {research.data.data.positions.map((p, i) => (
                  <li key={i}>{p.account_id}：{p.total_quantity} 股，成本 {p.avg_raw_cost}</li>
                ))}
              </ul>
            ) : <p className="body-copy muted">无持仓。</p>}
          </div>
        </div>
        {research.data?.data.as_of && (
          <p className="body-copy muted">数据日期：{research.data.data.as_of}（唯一模拟账本，只读展示）</p>
        )}
      </div>
    </div>
  );
}
