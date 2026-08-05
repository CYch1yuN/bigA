import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  api, WestockCapability, WestockRefreshJob, WestockRefreshRequest, WestockTarget,
} from '../api/client';
import { PageHeader } from '../components/ui';
import { formatTime, Metric } from '../dashboard/data';

const STATUS_TEXT: Record<string, string> = {
  fresh: '新鲜', stale: '已过期', unavailable: '无缓存', unsupported: '不支持',
};
const REQUEST_STATUS_TEXT: Record<string, string> = {
  pending: '等待处理', processing: '处理中', completed: '已完成',
  partial: '部分完成', failed: '失败', cancelled: '已取消', expired: '已过期',
};
const JOB_STATUS_TEXT: Record<string, string> = {
  pending: '等待', processing: '处理中', ok: '成功',
  partial: '部分', failed: '失败', skipped: '跳过',
};

function statusTone(status: string) {
  if (status === 'fresh') return 'success';
  if (status === 'stale') return 'warning';
  return 'neutral';
}

function requestTone(status: string) {
  if (status === 'completed') return 'success';
  if (status === 'partial' || status === 'processing') return 'warning';
  if (status === 'pending') return 'neutral';
  return 'danger';
}

function ageText(seconds: number | null) {
  if (seconds == null) return '暂无';
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时`;
}

// 与后端白名单一致（不提供 tool/scope/path 自由输入）
const STOCK_PRESETS = [
  { key: 'quote_only', label: '行情快照' },
  { key: 'basic', label: '基础（行情/资料/新闻/资金流）' },
  { key: 'market_data', label: '行情与技术' },
  { key: 'fundamentals', label: '财务与预期' },
  { key: 'ownership', label: '股东与分红' },
  { key: 'funds', label: '资金（含 LHB）' },
  { key: 'intel', label: '研报公告与风险事件' },
  { key: 'full_research', label: '全部个股研究能力' },
];
const MARKET_PRESETS = [
  { key: 'overview', label: '市场概览' },
  { key: 'structure', label: '板块/指数/产业链' },
  { key: 'macro', label: '宏观' },
  { key: 'funds', label: '市场资金' },
  { key: 'full_market', label: '全部市场能力' },
];

const SYMBOL_RE = /^[0-9]{6}\.(SH|SZ|BJ)$/;

export function WestockConnectionPage() {
  const client = useQueryClient();
  const [notice, setNotice] = useState('');
  const [noticeTone, setNoticeTone] = useState<'success' | 'error'>('success');
  // 受控表单
  const [target, setTarget] = useState('stock');
  const [preset, setPreset] = useState('basic');
  const [symbolsText, setSymbolsText] = useState('');
  const [allowSummary, setAllowSummary] = useState(false);
  const [resultId, setResultId] = useState('');
  // 分页
  const [offset, setOffset] = useState(0);
  const PAGE_SIZE = 5;

  const query = useQuery({
    queryKey: ['westock-connection'], queryFn: api.westockConnection, refetchInterval: 60_000,
  });
  const coverage = useQuery({
    queryKey: ['westock-coverage'], queryFn: () => api.westockCoverage(), refetchInterval: 60_000,
  });
  const requests = useQuery({
    queryKey: ['westock-refresh-requests', offset],
    queryFn: () => api.westockRefreshRequests({ limit: PAGE_SIZE, offset }),
    refetchInterval: 15_000,
  });

  const create = useMutation({
    mutationFn: (body: Parameters<typeof api.westockCreateRefreshRequest>[0]) =>
      api.westockCreateRefreshRequest(body),
    onSuccess: () => {
      setNotice('刷新请求已创建，等待 WorkBuddy 会话处理。');
      setNoticeTone('success');
      setSymbolsText('');
      setResultId('');
      void client.invalidateQueries({ queryKey: ['westock-refresh-requests'] });
      void client.invalidateQueries({ queryKey: ['westock-connection'] });
    },
    onError: () => {
      setNotice('刷新请求创建失败；请检查输入后重试。现有缓存与本地研究不受影响。');
      setNoticeTone('error');
    },
  });
  const cancel = useMutation({
    mutationFn: (requestId: string) => api.westockCancelRefreshRequest(requestId),
    onSuccess: () => {
      setNotice('请求已取消。');
      setNoticeTone('success');
      void client.invalidateQueries({ queryKey: ['westock-refresh-requests'] });
    },
    onError: () => setNotice('取消失败；请求可能已进入处理中。'),
  });

  const groups = useMemo(() => {
    const result = new Map<string, WestockCapability[]>();
    for (const item of query.data?.data.capabilities ?? []) {
      result.set(item.group, [...(result.get(item.group) ?? []), item]);
    }
    return [...result.entries()];
  }, [query.data]);

  const coverageData = coverage.data;
  const stockSymbols = useMemo(
    () => Object.keys(coverageData?.stock_matrix ?? {}).sort(),
    [coverageData],
  );
  const stockCaps = useMemo(() => {
    const set = new Set<string>();
    for (const sym of stockSymbols) {
      for (const cap of Object.keys(coverageData?.stock_matrix[sym] ?? {})) set.add(cap);
    }
    return [...set].sort();
  }, [coverageData, stockSymbols]);

  if (query.isLoading) return <div className="card loading-state">正在读取 Westock 连接状态…</div>;
  if (query.isError || !query.data) {
    return <div className="alert alert-error">无法读取 Westock 连接状态；本地研究主链不受影响。</div>;
  }
  const status = query.data;
  const requestsItems = requests.data?.items ?? [];
  const total = requests.data?.total ?? 0;
  const currentPresets = target === 'stock' ? STOCK_PRESETS : MARKET_PRESETS;

  const submitRefresh = () => {
    if (target === 'stock') {
      const symbols = symbolsText.split(/[,，\s]+/).filter(Boolean).map((s) => s.toUpperCase());
      const invalid = symbols.filter((s) => !SYMBOL_RE.test(s));
      if (symbols.length === 0 || symbols.length > 20) {
        setNotice('请填写 1–20 个股票代码。');
        setNoticeTone('error');
        return;
      }
      if (invalid.length > 0) {
        setNotice(`以下代码格式非法（需 600519.SH 格式，不自动转换）：${invalid[0]}`);
        setNoticeTone('error');
        return;
      }
      create.mutate({ target, preset, symbols, allow_summary_only: allowSummary });
      return;
    }
    if (target === 'market') {
      create.mutate({ target, preset });
      return;
    }
    if (!/^[0-9a-f]{32}$/.test(resultId)) {
      setNotice('筛选结果 ID 必须是 32 位小写 hex（scope 由后端从结果快照复算，不可手输）。');
      setNoticeTone('error');
      return;
    }
    create.mutate({ target, result_id: resultId });
  };

  const targetLabel = (t: WestockTarget) => {
    if (t.kind === 'stock') return `个股 ${t.symbols.join('、')} · ${t.preset ?? t.capabilities?.join(',') ?? ''}`;
    if (t.kind === 'market') return `市场 · ${t.preset}`;
    return `选股 · ${t.capability} (${t.result_id.slice(0, 8)}…)`;
  };
  const jobsText = (jobs: WestockRefreshJob[]) => {
    const done = jobs.filter((j) => j.status === 'ok').length;
    return `${done}/${jobs.length}`;
  };

  return (
    <div>
      <PageHeader title="Westock 数据连接" description="腾讯自选股研究数据旁路 · 不写入回测与模拟账户主链" />
      <div className="metric-grid">
        <Metric label="MCP 连接" value={status.availability.connected ? '已连接' : '未连接'} hint={status.transport === 'direct_mcp' ? 'MCP 直连' : '缓存导出桥'} tone={status.availability.connected ? 'success' : 'neutral'} />
        <Metric label="缓存可用" value={status.availability.cache_available ? '可用' : '不可用'} hint={status.cache_status === 'fresh' ? '含新鲜缓存' : status.cache_status === 'stale' ? '仅过期缓存' : '暂无缓存数据'} tone={status.availability.cache_available ? 'success' : 'warning'} />
        <Metric label="覆盖（新鲜/过期）" value={`${coverageData?.fresh_count ?? '—'}/${coverageData?.stale_count ?? '—'}`} hint={`本地股票 ${stockSymbols.length} 只 · 历史 ${coverageData?.local_history_available ? '可用' : '不可用'}`} tone={coverageData && coverageData.fresh_count ? 'success' : 'neutral'} />
        <Metric label="我的请求" value={requestsItems.filter((r) => r.status === 'pending').length} hint={`共 ${total} 条`} />
      </div>

      <div className="card section-card connection-summary">
        <div>
          <div className="card-title compact">连接说明</div>
          <p className="body-copy">Dashboard 独立进程没有 WorkBuddy Connector 的授权上下文，使用标准化缓存导出桥——MCP 连接为「未连接」，缓存可用性单独表达。刷新请求按 job（能力×范围）排队，由 WorkBuddy 会话认领处理。</p>
        </div>
      </div>
      {notice && <div className={`alert alert-${noticeTone}`} role="status">{notice}</div>}
      {status.warnings.map((warning) => <div className="alert alert-warning" key={warning}>{warning}</div>)}

      <div className="card section-card">
        <div className="card-title">创建刷新请求</div>
        <p className="body-copy muted">提交后进入排队（pending），由 WorkBuddy 会话认领处理；本页面不直接调用 MCP，也不提供 tool/scope/path 自由输入。</p>
        <div className="refresh-form-row">
          <label className="deep-field-label">目标
            <select value={target} onChange={(e) => { setTarget(e.target.value); setOffset(0); }}>
              <option value="stock">个股</option>
              <option value="market">市场</option>
              <option value="screener">选股结果</option>
            </select>
          </label>
          {target !== 'screener' && (
            <label className="deep-field-label">预设
              <select value={preset} onChange={(e) => setPreset(e.target.value)}>
                {currentPresets.map((p) => (
                  <option key={p.key} value={p.key}>{p.label}</option>
                ))}
              </select>
            </label>
          )}
          {target === 'stock' && (
            <>
              <label className="deep-field-label">股票代码（1–20 个）
                <input value={symbolsText} placeholder="600519.SH,000001.SZ" onChange={(e) => setSymbolsText(e.target.value)} />
              </label>
              <label className="deep-field-label checkbox-inline">
                <input type="checkbox" checked={allowSummary} onChange={(e) => setAllowSummary(e.target.checked)} />
                允许非本地股票摘要（summary-only）
              </label>
            </>
          )}
          {target === 'screener' && (
            <label className="deep-field-label">筛选结果 ID（32 位 hex）
              <input value={resultId} placeholder="从筛选结果快照复制" onChange={(e) => setResultId(e.target.value)} />
            </label>
          )}
          <button className="btn" disabled={create.isPending} onClick={submitRefresh}>
            {create.isPending ? '提交中…' : '创建刷新请求'}
          </button>
        </div>
      </div>

      <div className="card section-card">
        <div className="card-title">刷新请求队列（当前会话）</div>
        {requestsItems.length === 0 ? <p className="body-copy muted">暂无刷新请求。</p> : (
          <div className="table-wrap">
            <table className="table connection-table">
              <thead><tr><th>请求 ID</th><th>目标</th><th>状态</th><th>Jobs 进度</th><th>创建时间</th><th>说明</th><th>操作</th></tr></thead>
              <tbody>{requestsItems.map((item: WestockRefreshRequest) => (
                <tr key={item.request_id}>
                  <td><code className="req-id">{item.request_id}</code></td>
                  <td>{targetLabel(item.target)}</td>
                  <td><span className={`badge badge-${requestTone(item.status)}`}>{REQUEST_STATUS_TEXT[item.status] ?? item.status}</span></td>
                  <td>
                    <span className="muted">{jobsText(item.jobs)}</span>
                    <details className="muted jobs-detail">
                      <summary>明细</summary>
                      {item.jobs.map((j) => (
                        <div key={j.job_id} className="deep-chip-row">
                          <span className="deep-chip">{j.capability}·{j.scope === 'global' ? 'global' : j.scope.slice(0, 8)}: {JOB_STATUS_TEXT[j.status] ?? j.status}</span>
                        </div>
                      ))}
                    </details>
                  </td>
                  <td>{formatTime(item.created_at)}</td>
                  <td className="muted">{item.status_detail ?? ((item.warnings ?? []).join('；') || '—')}</td>
                  <td>{item.status === 'pending' ? (
                    <button className="btn btn-sm" disabled={cancel.isPending} onClick={() => cancel.mutate(item.request_id)}>取消</button>
                  ) : null}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
        <div className="stock-chart-controls">
          <div className="btn-group">
            <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</button>
            <button className="btn btn-sm" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
          </div>
          <span className="muted">第 {Math.floor(offset / PAGE_SIZE) + 1} 页 · 共 {total} 条</span>
        </div>
      </div>

      <div className="card section-card">
        <div className="card-title">缓存覆盖矩阵（能力缺口标红）</div>
        {coverageData && stockSymbols.length > 0 ? (
          <div className="table-wrap">
            <table className="table connection-table">
              <thead><tr><th>股票</th><th>本地历史</th>{stockCaps.map((cap) => <th key={cap}>{cap}</th>)}</tr></thead>
              <tbody>{stockSymbols.map((sym) => (
                <tr key={sym}>
                  <td><strong>{sym}</strong></td>
                  <td>{coverageData.stock_local_history[sym] ? '✓' : '—'}</td>
                  {stockCaps.map((cap) => {
                    const s = coverageData.stock_matrix[sym]?.[cap];
                    return <td key={cap}><span className={`badge badge-${statusTone(s ?? 'unavailable')}`}>{STATUS_TEXT[s ?? 'unavailable']}</span></td>;
                  })}
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <p className="body-copy muted">暂无股票级缓存覆盖。</p>}
        {coverageData && Object.keys(coverageData.global_capabilities).length > 0 && (
          <div className="muted">全局能力：{Object.entries(coverageData.global_capabilities)
            .map(([cap, s]) => `${cap}(${STATUS_TEXT[s] ?? s})`).join('、')}</div>
        )}
        {coverageData && coverageData.latest_export_at ? (
          <div className="muted">最近成功导出：{formatTime(coverageData.latest_export_at)}</div>
        ) : null}
        {coverageData?.warnings?.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
      </div>

      <div className="connection-groups">
        {groups.map(([group, capabilities]) => (
          <section className="card connection-group" key={group}>
            <div className="connection-group-header">
              <div><div className="card-title compact">{group}</div><div className="muted">{capabilities.length} 项只读研究能力</div></div>
            </div>
            <div className="table-wrap">
              <table className="table connection-table">
                <thead><tr><th>能力</th><th>状态</th><th>缓存年龄</th><th>最近成功</th></tr></thead>
                <tbody>{capabilities.map((item) => (
                  <tr key={item.name}>
                    <td><strong>{item.name}</strong></td>
                    <td><span className={`badge badge-${statusTone(item.status)}`}>{STATUS_TEXT[item.status] ?? item.status}</span></td>
                    <td>{ageText(item.cache_age_seconds)}</td><td>{formatTime(item.last_success_at)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </section>
        ))}
      </div>

      <div className="card section-card">
        <div className="card-title">数据边界</div>
        <ul className="connection-boundaries">
          <li>本地 curated 是历史行情、复权、指标计算和回测的唯一权威来源。</li>
          <li>Westock 数据只用于研究展示、资讯补充与交叉核验；qfq/hfq 不进入 K 线、策略、订单或 Gate 4B。</li>
          <li>不接入 Westock 模拟交易；BigA 模拟账户保持唯一账本。</li>
          <li>刷新请求由 WorkBuddy 会话认领处理；Dashboard 不直连 MCP。</li>
          <li>选股请求的 scope 由后端从结果快照复算，前端不允许手输。</li>
        </ul>
      </div>
    </div>
  );
}
