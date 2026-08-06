import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import {
  api,
  WestockAlert,
  WestockCapability,
  WestockHealthData,
  WestockOpsCache,
  WestockOpsCapability,
  WestockOpsEnvelope,
  WestockOpsRequest,
  WestockOpsSummaryData,
  WestockOpsSymbol,
  WestockRecommendation,
  WestockRefreshJob,
  WestockRefreshRequest,
  WestockTarget,
  WestockTrendsData,
} from '../api/client';
import { PageHeader } from '../components/ui';
import { fmtIsoTime } from '../components/StrongCards';
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
const FRESH_TEXT: Record<string, string> = {
  fresh: '新鲜', stale: '已过期', future_timestamp: '未来时间', invalid_timestamp: '时间无效',
  unavailable: '无数据',
};
const FILE_STATE_TEXT: Record<string, string> = {
  missing: '缺失', invalid_json: 'JSON 损坏', invalid_envelope: '信封非法',
  invalid_timestamp: '时间无效', future_timestamp: '未来时间', valid: '正常',
};
const HASH_TEXT: Record<string, string> = {
  verified: '已验证', unverified: '未验证', mismatch: '不一致', pending_evidence: '证据待定',
};
const RECEIPT_TEXT: Record<string, string> = {
  valid: '回执有效', missing: '回执缺失', invalid: '回执非法',
  mismatch: '回执不一致', not_applicable: '无需回执',
};
const STATUS_DETAIL_TEXT: Record<string, string> = {
  completed_all: '全部完成', partial_success: '部分完成', failed_none: '未成功导出',
  worker_timeout: 'Worker 超时', receipt_failed: '回执写入失败',
  pending: '等待处理', processing: '处理中', cancelled: '已取消', expired: '已过期',
  unknown: '未知',
};
const shortQ = (v: string) => (v.length > 20 ? `${v.slice(0, 7)}…${v.slice(-4)}` : v);
const reqSymbolsText = (s: string | string[] | null) => {
  if (s == null) return '';
  const list = Array.isArray(s) ? s : [s];
  return list.map((x) => (x.startsWith('q_') ? shortQ(x) : x)).join(',');
};
// F5-B 健康/告警/建议文本映射
const HEALTH_STATUS_TEXT: Record<string, string> = {
  critical: '严重', degraded: '降级', attention: '注意', healthy: '健康', not_observed: '未观测',
};
const SEVERITY_TEXT: Record<string, string> = {
  critical: '严重', high: '高', medium: '中', low: '低',
};
const ALERT_CAT_TEXT: Record<string, string> = {
  hash_mismatch: '哈希不一致', consumer_unusable: '消费者不可用', receipt_mismatch: '回执不一致',
  invalid_cache_file: '缓存文件非法', future_timestamp: '未来时间戳', receipt_invalid: '回执非法',
  recent_worker_timeout: 'Worker 超时', recent_refresh_failure: '刷新失败',
  receipt_missing: '回执缺失', orphan_receipt: '孤立回执', invalid_receipt_file: '非法回执文件',
  stale_cache: '缓存过期', missing_expected_cache: '预期缓存缺失', low_valid_coverage: '覆盖偏低',
  capability_unavailable: '能力不可用', hash_unverified: '哈希未验证', pending_evidence: '证据待定',
  partial_refresh: '部分刷新', unsupported_or_empty: '不支持或为空',
};
const REC_CODE_TEXT: Record<string, string> = {
  refresh_invalid_cache: '刷新非法缓存', refresh_hash_mismatch: '刷新并核对哈希',
  refresh_stale_capability: '刷新过期能力', refresh_missing_coverage: '补齐缺失覆盖',
  retry_recent_failure: '重试最近失败', inspect_receipt_chain: '核查回执审计链',
  inspect_consumer_schema: '核查数据模式', rerun_screener_export: '重新导出筛选结果',
  no_action_required: '无需操作',
};
const severityTone = (s: string) => {
  if (s === 'critical') return 'danger';
  if (s === 'high') return 'warning';
  if (s === 'medium') return 'warning';
  return 'neutral';
};
const CONSUMER_TEXT: Record<string, string> = {
  usable: '可用', unusable: '不可用', not_validated: '未校验',
};
const FAIL_LABEL: Record<string, string> = {
  upstream_empty: '上游无数据', upstream_rate_limited: '上游限频', upstream_unavailable: '上游不可用',
  unsupported: '能力不支持', consumer_validation_failed: '消费者校验失败', identity_mismatch: '身份不匹配',
  invalid_envelope: '缓存信封非法', stale: '缓存过期', future_timestamp: '未来时间戳', worker_timeout: 'Worker 超时',
  export_failed: '导出失败', receipt_failed: '回执失败', cancelled: '已取消', expired: '已过期', unknown: '未知',
};
// capability 受控中文名（不展示 MCP tool 名）
const CAP_LABEL: Record<string, string> = {
  quote: '行情', minute: '分时', technical: '技术指标', profile: '公司资料', financials: '财务',
  forecast: '一致预期', shareholders: '股东', dividend: '分红', buyback: '回购', margin: '融资融券',
  block_trade: '大宗交易', fund_flow: '资金流', northbound: '北向持股', news: '新闻', reports: '研报',
  announcements: '公告', events: '事件', risk: '风险', lhb: '龙虎榜', chip_distribution: '筹码分布',
  market_overview: '市场概览', change_distribution: '涨跌分布', hot_ranking: '热度排行', sector: '板块',
  index: '指数', industry_chain: '产业链', macro: '宏观', filter: '条件选股', strategy_select: '策略选股',
  factor_ranking: '因子排行', label_select: '标签选股', watchlist: '自选股',
};
const capLabel = (cap: string) => `${CAP_LABEL[cap] ?? cap}`;

function statusTone(status: string) {
  if (status === 'fresh' || status === 'usable' || status === 'ok') return 'success';
  if (status === 'stale' || status === 'partial' || status === 'processing') return 'warning';
  return 'neutral';
}
function requestTone(status: string) {
  if (status === 'completed') return 'success';
  if (status === 'partial' || status === 'processing') return 'warning';
  if (status === 'pending') return 'neutral';
  return 'danger';
}
function ageText(seconds: number | null) {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时`;
}
const durationText = (s: number | null) => {
  if (s == null) return '—';
  if (s < 60) return `${s} 秒`;
  return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
};
const pct = (v: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);

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
// 个股能力注册表（与后端 _STOCK_CAPS 一一对应；capabilities 模式只允许这些键）
const STOCK_CAPABILITIES = [
  { key: 'quote', label: '行情快照' },
  { key: 'minute', label: '分时' },
  { key: 'technical', label: '技术指标' },
  { key: 'profile', label: '公司资料' },
  { key: 'financials', label: '财务' },
  { key: 'forecast', label: '盈利预测' },
  { key: 'shareholders', label: '股东' },
  { key: 'dividend', label: '分红' },
  { key: 'buyback', label: '回购' },
  { key: 'margin', label: '融资融券' },
  { key: 'block_trade', label: '大宗交易' },
  { key: 'fund_flow', label: '资金流' },
  { key: 'northbound', label: '北向持股' },
  { key: 'news', label: '新闻' },
  { key: 'reports', label: '研报' },
  { key: 'announcements', label: '公告' },
  { key: 'events', label: '事件' },
  { key: 'risk', label: '风险' },
  { key: 'lhb', label: '龙虎榜' },
  { key: 'chip_distribution', label: '筹码分布' },
] as const;
const STOCK_CAP_KEYS: readonly string[] = STOCK_CAPABILITIES.map((c) => c.key);
const MAX_CAPABILITIES = 20;

const SYMBOL_RE = /^[0-9]{6}\.(SH|SZ|BJ)$/;

const TABS = [
  { key: 'connection', label: '连接状态' },
  { key: 'caches', label: '缓存质量' },
  { key: 'capabilities', label: '能力覆盖' },
  { key: 'symbols', label: '股票覆盖' },
  { key: 'history', label: '刷新历史' },
  { key: 'failures', label: '失败分析' },
  { key: 'health', label: '健康状态' },
  { key: 'alerts', label: '活动告警' },
  { key: 'recommendations', label: '维护建议' },
  { key: 'trends', label: '刷新趋势' },
] as const;
type TabKey = (typeof TABS)[number]['key'];

/** F5-A 运营只读 hook：首次激活才请求；staleTime 5 分钟；返回旧 Tab 不重复请求。 */
function useOps<T>(key: string, fn: () => Promise<WestockOpsEnvelope<T>>, active: boolean) {
  return useQuery({
    queryKey: ['westock-ops', key],
    queryFn: fn,
    enabled: active,
    staleTime: 5 * 60_000,
  });
}

function OpsEmpty({ text }: { text: string }) {
  return <p className="body-copy muted">{text}</p>;
}

// ------------------------------------------------------------------ //
// Tab：缓存质量
// ------------------------------------------------------------------ //
function CachesTab({ active }: { active: boolean }) {
  const [offset, setOffset] = useState(0);
  const PAGE = 20;
  const q = useOps<{
    total: number; coverage_total: number; inventory_total: number;
    unexpected_physical_count: number;
    limit: number; offset: number; items: WestockOpsCache[];
  }>(`caches-${offset}`, () => api.westockOpsCaches({ limit: PAGE, offset }), active);
  const items = q.data?.data.items ?? [];
  const coverageTotal = q.data?.data.coverage_total ?? 0;
  const inventoryTotal = q.data?.data.inventory_total ?? 0;
  const unexpected = q.data?.data.unexpected_physical_count ?? 0;
  const pageTotal = q.data?.data.total ?? 0; // 过滤后 total（翻页依据）
  const scopeText = (item: WestockOpsCache) => {
    if (item.scope === 'invalid_scope') return `非法 Scope${item.scope_id ? `（${item.scope_id}）` : ''}`;
    if (item.scope_type === 'query') return item.short_scope;
    return item.scope === 'global' ? 'global' : item.scope;
  };
  return (
    <div className="card section-card">
      <div className="card-title">缓存质量</div>
      <p className="body-copy muted">
        预期单元 {coverageTotal} · 物理文件 {inventoryTotal}（意外 {unexpected}）。
      </p>
      <p className="body-copy muted">覆盖矩阵含缺失单元；fresh/stale 只描述时间，consumer 可用性复用正式受控标准化判定。</p>
      {q.isLoading ? <div className="card loading-state">正在读取缓存质量…</div>
        : q.isError ? <div className="alert alert-error">缓存质量读取失败；不影响本地研究主链。</div>
          : items.length === 0 ? <OpsEmpty text="暂无缓存条目。" /> : (
            <div className="table-wrap">
              <table className="table connection-table">
                <thead><tr>
                  <th>能力</th><th>Scope</th><th>文件</th><th>时间</th><th>可用性</th>
                  <th>哈希</th><th>数据日期</th><th>抓取时间</th><th>缓存年龄</th><th>TTL</th>
                </tr></thead>
                <tbody>{items.map((item) => (
                  <tr key={`${item.capability}:${item.scope_id ?? item.scope}`}>
                    <td><strong>{capLabel(item.capability)}</strong> <span className="muted">{item.capability}</span></td>
                    <td><code className="req-id">{scopeText(item)}</code></td>
                    <td>
                      <span className={`badge ${item.file_state === 'valid' ? 'badge-success' : item.file_state === 'missing' ? 'badge-danger' : 'badge-warning'}`}>{FILE_STATE_TEXT[item.file_state] ?? item.file_state}</span>
                      {!item.in_expected_matrix && <span className="badge badge-neutral">意外</span>}
                    </td>
                    <td>
                      <span className={`badge badge-${statusTone(item.freshness_status ?? 'unavailable')}`}>{FRESH_TEXT[item.freshness_status ?? 'unavailable'] ?? item.freshness_status ?? '—'}</span>
                      {' '}<span className={`badge ${item.consumer_status === 'usable' ? 'badge-success' : item.consumer_status === 'unusable' ? 'badge-danger' : 'badge-neutral'}`}>{CONSUMER_TEXT[item.consumer_status] ?? item.consumer_status}</span>
                    </td>
                    <td className="muted">{item.availability === 'available' ? '可用' : '不可用'}</td>
                    <td className="muted">{item.integrity.valid ? HASH_TEXT[item.integrity.hash_status] ?? item.integrity.hash_status : '—'}</td>
                    <td>{item.as_of ?? '—'}</td>
                    <td>{fmtIsoTime(item.fetched_at)}</td>
                    <td>{ageText(item.age_seconds)}</td>
                    <td className="muted">{item.ttl_seconds ? ageText(item.ttl_seconds) : '—'}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
      <div className="stock-chart-controls">
        <div className="btn-group">
          <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
          <button className="btn btn-sm" disabled={offset + PAGE >= pageTotal} onClick={() => setOffset(offset + PAGE)}>下一页</button>
        </div>
        <span className="muted">第 {Math.floor(offset / PAGE) + 1} 页 · 共 {pageTotal} 条</span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ //
// Tab：能力覆盖
// ------------------------------------------------------------------ //
function CapabilitiesTab({ active }: { active: boolean }) {
  const [offset, setOffset] = useState(0);
  const [capFilter, setCapFilter] = useState('');
  const PAGE = 20;
  const q = useOps<WestockOpsSummaryData>('summary', api.westockOpsSummary, active);
  const list = useOps<{ total: number; limit: number; offset: number; items: WestockOpsCapability[] }>(
    `caps-${offset}-${capFilter}`,
    () => api.westockOpsCapabilities({ capability: capFilter || undefined, limit: PAGE, offset }),
    active);
  const d = q.data?.data;
  const items = list.data?.data.items ?? [];
  const total = list.data?.data.total ?? 0;
  return (
    <div className="card section-card">
      <div className="card-title">能力覆盖</div>
      {q.isLoading ? <div className="card loading-state">正在读取能力覆盖…</div>
        : q.isError ? <div className="alert alert-error">能力覆盖读取失败；不影响本地研究主链。</div>
          : !d ? <OpsEmpty text="暂无覆盖数据。" /> : (
            <>
              <div className="metric-grid">
                <Metric label="预期覆盖单元" value={d.expected_cell_count} hint={`可用 ${d.availability.available} · 缺失/不可用 ${d.availability.unavailable}`} tone={d.availability.unavailable === 0 ? 'success' : 'warning'} />
                <Metric label="物理缓存文件" value={d.physical_cache_count} hint={`总单元 ${d.total_cells}`} tone="neutral" />
                <Metric label="意外文件" value={d.unexpected_physical_count} hint="预期矩阵之外的实际文件" tone={d.unexpected_physical_count ? 'warning' : 'neutral'} />
                <Metric label="无效物理文件" value={d.invalid_physical_count} hint="坏 JSON/信封/时间戳文件" tone={d.invalid_physical_count ? 'danger' : 'neutral'} />
                <Metric label="有效覆盖率" value={pct(d.valid_coverage)} hint={`可用且新鲜/过期 ${d.usable_fresh_stale} / 应有 ${d.expected_cell_count}`} tone={d.valid_coverage && d.valid_coverage > 0.5 ? 'success' : 'warning'} />
                <Metric label="哈希不一致" value={d.integrity.hash_mismatch} hint={`未验证 ${d.integrity.hash_unverified} · 证据待定 ${d.integrity.pending_evidence}`} tone={d.integrity.hash_mismatch ? 'danger' : 'neutral'} />
              </div>
              <div className="refresh-form-row">
                <label className="deep-field-label">能力过滤
                  <input value={capFilter} placeholder="如 quote" onChange={(e) => { setCapFilter(e.target.value.trim()); setOffset(0); }} />
                </label>
              </div>
              {items.length === 0 ? <OpsEmpty text="暂无能力统计。" /> : (
                <div className="table-wrap">
                  <table className="table connection-table">
                    <thead><tr><th>能力</th><th>分组</th><th>Scope 数</th><th>可用</th><th>已过期</th><th>不可用</th><th>成功率</th><th>最近成功</th><th>最近失败</th></tr></thead>
                    <tbody>{items.map((c) => (
                      <tr key={c.capability}>
                        <td><strong>{capLabel(c.capability)}</strong> <span className="muted">{c.capability}</span></td>
                        <td className="muted">{c.group}</td>
                        <td>{c.scope_count}</td>
                        <td><span className="badge badge-success">{c.usable}</span></td>
                        <td><span className="badge badge-warning">{c.stale}</span></td>
                        <td><span className="badge badge-neutral">{c.unavailable}</span></td>
                        <td>{pct(c.success_rate)}</td>
                        <td>{fmtIsoTime(c.latest_ok_at)}</td>
                        <td>{fmtIsoTime(c.latest_fail_at)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
              <div className="stock-chart-controls">
                <div className="btn-group">
                  <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
                  <button className="btn btn-sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>下一页</button>
                </div>
                <span className="muted">第 {Math.floor(offset / PAGE) + 1} 页 · 共 {total} 条</span>
              </div>
            </>
          )}
    </div>
  );
}

// ------------------------------------------------------------------ //
// Tab：股票覆盖
// ------------------------------------------------------------------ //
function SymbolsTab({ active }: { active: boolean }) {
  const [offset, setOffset] = useState(0);
  const [symFilter, setSymFilter] = useState('');
  const PAGE = 20;
  const q = useOps<{ total: number; limit: number; offset: number; items: WestockOpsSymbol[] }>(
    `symbols-${offset}-${symFilter}`,
    () => api.westockOpsSymbols({ symbol: symFilter || undefined, limit: PAGE, offset }),
    active);
  const items = q.data?.data.items ?? [];
  const total = q.data?.data.total ?? 0;
  return (
    <div className="card section-card">
      <div className="card-title">股票覆盖</div>
      <p className="body-copy muted">非本地股票（无本地 K 线）为摘要覆盖：minute/technical 不计入应有集合。</p>
      {q.isLoading ? <div className="card loading-state">正在读取股票覆盖…</div>
        : q.isError ? <div className="alert alert-error">股票覆盖读取失败；不影响本地研究主链。</div>
          : (
            <>
              <div className="refresh-form-row">
                <label className="deep-field-label">股票过滤
                  <input value={symFilter} placeholder="600519.SH" onChange={(e) => { setSymFilter(e.target.value.trim()); setOffset(0); }} />
                </label>
              </div>
              {items.length === 0 ? <OpsEmpty text="暂无股票级缓存。" /> : (
                <div className="table-wrap">
                  <table className="table connection-table">
                    <thead><tr><th>股票</th><th>本地 K 线</th><th>有效覆盖/应有</th><th>已过期</th><th>不可用</th></tr></thead>
                    <tbody>{items.map((s) => (
                      <tr key={s.symbol}>
                        <td><strong>{s.symbol}</strong></td>
                        <td>{s.local_history_available ? <span className="badge badge-success">可用</span> : <span className="badge badge-neutral">尚未补跑</span>}</td>
                        <td>{s.usable}<span className="muted">/{s.expected_count}</span></td>
                        <td><span className="badge badge-warning">{s.stale}</span></td>
                        <td><span className="badge badge-neutral">{s.unavailable}</span></td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
              <div className="stock-chart-controls">
                <div className="btn-group">
                  <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
                  <button className="btn btn-sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>下一页</button>
                </div>
                <span className="muted">第 {Math.floor(offset / PAGE) + 1} 页 · 共 {total} 条</span>
              </div>
            </>
          )}
    </div>
  );
}

// ------------------------------------------------------------------ //
// Tab：刷新历史
// ------------------------------------------------------------------ //
function HistoryTab({ active }: { active: boolean }) {
  const [offset, setOffset] = useState(0);
  const PAGE = 10;
  const q = useOps<{ total: number; limit: number; offset: number; items: WestockOpsRequest[] }>(
    `history-${offset}`, () => api.westockOpsRequests({ limit: PAGE, offset }), active);
  const items = q.data?.data.items ?? [];
  const total = q.data?.data.total ?? 0;
  const jobsText = (j: WestockOpsRequest['job_counts']) =>
    `成功 ${j.ok} · 部分 ${j.partial} · 失败 ${j.failed} · 跳过 ${j.skipped}`;
  const warnText = (r: WestockOpsRequest) => {
    if (r.warning_count === 0) return '—';
    const cats = Object.entries(r.warning_categories).filter(([, n]) => n > 0);
    return `${r.warning_count} 条${cats.length ? `（${cats.map(([c, n]) => `${FAIL_LABEL[c] ?? c} ${n}`).join('，')}）` : ''}`;
  };
  return (
    <div className="card section-card">
      <div className="card-title">刷新历史（只读）</div>
      <p className="body-copy muted">基于已有 request/receipt 的只读视图；warning 只显示固定分类，不展示原始文本。</p>
      {q.isLoading ? <div className="card loading-state">正在读取刷新历史…</div>
        : q.isError ? <div className="alert alert-error">刷新历史读取失败；不影响本地研究主链。</div>
          : items.length === 0 ? <OpsEmpty text="暂无刷新请求。" /> : (
            <div className="table-wrap">
              <table className="table connection-table">
                <thead><tr><th>请求 ID</th><th>目标</th><th>状态</th><th>回执</th><th>Job 统计</th><th>警告</th><th>开始</th><th>完成</th><th>耗时</th></tr></thead>
                <tbody>{items.map((r) => (
                  <tr key={r.request_id}>
                    <td><code className="req-id">{r.short_id}…</code></td>
                    <td className="muted">{r.target ?? '—'}{r.preset ? ` · ${r.preset}` : ''}{reqSymbolsText(r.symbols) ? ` · ${reqSymbolsText(r.symbols)}` : ''}</td>
                    <td>
                      <span className={`badge badge-${requestTone(r.status)}`}>{REQUEST_STATUS_TEXT[r.status] ?? r.status}</span>
                      {STATUS_DETAIL_TEXT[r.status_detail_code] && <span className="muted"> · {STATUS_DETAIL_TEXT[r.status_detail_code]}</span>}
                    </td>
                    <td><span className={`badge ${r.receipt_status === 'valid' ? 'badge-success' : r.receipt_status === 'not_applicable' ? 'badge-neutral' : 'badge-warning'}`}>{RECEIPT_TEXT[r.receipt_status] ?? r.receipt_status}</span></td>
                    <td className="muted">{jobsText(r.job_counts)}</td>
                    <td className="muted">{warnText(r)}</td>
                    <td>{fmtIsoTime(r.started_at ?? r.created_at)}</td>
                    <td>{fmtIsoTime(r.finished_at)}</td>
                    <td>{durationText(r.duration_seconds)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
      <div className="stock-chart-controls">
        <div className="btn-group">
          <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
          <button className="btn btn-sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>下一页</button>
        </div>
        <span className="muted">第 {Math.floor(offset / PAGE) + 1} 页 · 共 {total} 条</span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ //
// Tab：失败分析
// ------------------------------------------------------------------ //
function FailuresTab({ active }: { active: boolean }) {
  const q = useOps<{
    job_failure_categories: Record<string, number>;
    request_failure_categories: Record<string, number>;
    failed_job_count: number;
    failed_request_count: number;
    receipt_audit_issues: Record<string, number>;
    receipt_audit_issue_count: number;
    orphan_receipt_count: number;
    invalid_receipt_file_count: number;
  }>('failures', api.westockOpsFailures, active);
  const d = q.data?.data;
  const jobRows = d ? Object.entries(d.job_failure_categories).filter(([, n]) => n > 0) : [];
  const reqRows = d ? Object.entries(d.request_failure_categories).filter(([, n]) => n > 0) : [];
  const receiptRows = d ? Object.entries(d.receipt_audit_issues).filter(([, n]) => n > 0) : [];
  return (
    <div className="card section-card">
      <div className="card-title">失败分析（固定分类）</div>
      <p className="body-copy muted">分类来自请求/job 固定状态与脱敏 warning 的受控映射；失败任务与失败请求分开统计，不混加。</p>
      {q.isLoading ? <div className="card loading-state">正在读取失败分析…</div>
        : q.isError ? <div className="alert alert-error">失败分析读取失败；不影响本地研究主链。</div>
          : (
            <>
              <div className="metric-grid">
                <Metric label="失败任务" value={d?.failed_job_count ?? '—'} hint="失败的 job 数" tone={d?.failed_job_count ? 'warning' : 'neutral'} />
                <Metric label="失败请求" value={d?.failed_request_count ?? '—'} hint="失败的 request 数" tone={d?.failed_request_count ? 'danger' : 'neutral'} />
                <Metric label="回执审计异常" value={d?.receipt_audit_issue_count ?? '—'} hint="终态请求缺/坏/不一致回执" tone={d?.receipt_audit_issue_count ? 'danger' : 'neutral'} />
                <Metric label="孤立回执" value={d?.orphan_receipt_count ?? '—'} hint="无对应请求的回执文件" tone={d?.orphan_receipt_count ? 'warning' : 'neutral'} />
                <Metric label="非法回执文件" value={d?.invalid_receipt_file_count ?? '—'} hint="存在但校验失败的回执" tone={d?.invalid_receipt_file_count ? 'warning' : 'neutral'} />
              </div>
              <div className="card-title compact">失败任务分类</div>
              {jobRows.length === 0 ? <OpsEmpty text="暂无失败任务。" /> : (
                <div className="table-wrap">
                  <table className="table connection-table">
                    <thead><tr><th>分类</th><th>数量</th></tr></thead>
                    <tbody>{jobRows.map(([cat, n]) => (
                      <tr key={`job-${cat}`}>
                        <td><strong>{FAIL_LABEL[cat] ?? cat}</strong></td>
                        <td><span className="badge badge-neutral">{n}</span></td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
              <div className="card-title compact">失败请求分类</div>
              {reqRows.length === 0 ? <OpsEmpty text="暂无失败请求。" /> : (
                <div className="table-wrap">
                  <table className="table connection-table">
                    <thead><tr><th>分类</th><th>数量</th></tr></thead>
                    <tbody>{reqRows.map(([cat, n]) => (
                      <tr key={`req-${cat}`}>
                        <td><strong>{FAIL_LABEL[cat] ?? cat}</strong></td>
                        <td><span className="badge badge-neutral">{n}</span></td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
              <div className="card-title compact">回执审计异常明细</div>
              {receiptRows.length === 0 ? <OpsEmpty text="回执审计正常。" /> : (
                <div className="table-wrap">
                  <table className="table connection-table">
                    <thead><tr><th>类型</th><th>数量</th></tr></thead>
                    <tbody>{receiptRows.map(([cat, n]) => (
                      <tr key={`receipt-${cat}`}>
                        <td><strong>{RECEIPT_TEXT[cat] ?? cat}</strong></td>
                        <td><span className="badge badge-neutral">{n}</span></td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
            </>
          )}
    </div>
  );
}

// ------------------------------------------------------------------ //
// F5-B Tab：健康状态
// ------------------------------------------------------------------ //
function HealthTab({ active }: { active: boolean }) {
  const q = useOps<WestockHealthData>('health', api.westockHealth, active);
  const d = q.data?.data;
  const dimOrder = ['integrity', 'consumer', 'freshness', 'coverage', 'refresh_workflow'] as const;
  const dimLabel: Record<string, string> = {
    integrity: '完整性', consumer: '消费者', freshness: '时效', coverage: '覆盖', refresh_workflow: '刷新闭环',
  };
  return (
    <div className="card section-card">
      <div className="card-title">健康状态</div>
      {q.isLoading ? <div className="card loading-state">正在评估健康状态…</div>
        : q.isError ? <div className="alert alert-error">健康状态读取失败；不影响本地研究主链。</div>
          : !d ? <OpsEmpty text="暂无健康数据。" /> : (
            <>
              <div className="metric-grid">
                <Metric label="总体状态" value={HEALTH_STATUS_TEXT[d.overall_status] ?? d.overall_status}
                  hint={d.note} tone={d.overall_status === 'critical' ? 'danger' : d.overall_status === 'degraded' ? 'warning' : d.overall_status === 'attention' ? 'warning' : 'neutral'} />
                {(['critical', 'high', 'medium', 'low'] as const).map((s) => (
                  <Metric key={s} label={`${SEVERITY_TEXT[s]}级告警`} value={d.alert_summary[s] ?? 0} tone={s === 'critical' ? 'danger' : s === 'high' ? 'warning' : 'neutral'} />
                ))}
              </div>
              <div className="card-title compact">五个维度</div>
              <div className="table-wrap">
                <table className="table connection-table">
                  <thead><tr><th>维度</th><th>状态</th><th>说明</th><th>告警数</th></tr></thead>
                  <tbody>{dimOrder.map((dim) => {
                    const v = d.dimensions[dim];
                    if (!v) return null;
                    return (
                      <tr key={dim}>
                        <td><strong>{dimLabel[dim]}</strong></td>
                        <td><span className={`badge badge-${v.status === 'critical' ? 'danger' : v.status === 'healthy' ? 'success' : 'warning'}`}>{HEALTH_STATUS_TEXT[v.status] ?? v.status}</span></td>
                        <td className="muted">{v.explanation}</td>
                        <td><span className="badge badge-neutral">{v.alert_count}</span></td>
                      </tr>
                    );
                  })}</tbody>
                </table>
              </div>
              <p className="body-copy muted">Westock 异常不影响本地 curated、回测与模拟账本。</p>
            </>
          )}
    </div>
  );
}

// ------------------------------------------------------------------ //
// F5-B Tab：活动告警
// ------------------------------------------------------------------ //
function AlertsTab({ active }: { active: boolean }) {
  const [offset, setOffset] = useState(0);
  const [sevFilter, setSevFilter] = useState('');
  const [catFilter, setCatFilter] = useState('');
  const [capFilter, setCapFilter] = useState('');
  const PAGE = 20;
  const q = useOps<{ total: number; limit: number; offset: number; items: WestockAlert[] }>(
    `alerts-${offset}-${sevFilter}-${catFilter}-${capFilter}`,
    () => api.westockAlerts({
      severity: sevFilter || undefined, category: catFilter || undefined,
      capability: capFilter || undefined, limit: PAGE, offset,
    }),
    active);
  const items = q.data?.data.items ?? [];
  const total = q.data?.data.total ?? 0;
  return (
    <div className="card section-card">
      <div className="card-title">活动告警</div>
      <p className="body-copy muted">告警由固定规则生成，仅展示受控信息，不包含原始 warning 或完整 scope。</p>
      <div className="refresh-form-row">
        <label className="deep-field-label">严重度
          <select value={sevFilter} onChange={(e) => { setSevFilter(e.target.value); setOffset(0); }}>
            <option value="">全部</option>
            {(['critical', 'high', 'medium', 'low'] as const).map((s) => (
              <option key={s} value={s}>{SEVERITY_TEXT[s]}</option>
            ))}
          </select>
        </label>
        <label className="deep-field-label">类型
          <input value={catFilter} placeholder="如 hash_mismatch" onChange={(e) => { setCatFilter(e.target.value.trim()); setOffset(0); }} />
        </label>
        <label className="deep-field-label">能力
          <input value={capFilter} placeholder="如 quote" onChange={(e) => { setCapFilter(e.target.value.trim()); setOffset(0); }} />
        </label>
      </div>
      {q.isLoading ? <div className="card loading-state">正在读取告警…</div>
        : q.isError ? <div className="alert alert-error">告警读取失败；不影响本地研究主链。</div>
          : items.length === 0 ? <OpsEmpty text="暂无活动告警。" /> : (
            <div className="table-wrap">
              <table className="table connection-table">
                <thead><tr><th>级别</th><th>类型</th><th>标题</th><th>能力</th><th>影响</th><th>说明</th></tr></thead>
                <tbody>{items.map((a) => (
                  <tr key={a.alert_id}>
                    <td><span className={`badge badge-${severityTone(a.severity)}`}>{SEVERITY_TEXT[a.severity] ?? a.severity}</span></td>
                    <td><strong>{ALERT_CAT_TEXT[a.category] ?? a.category}</strong></td>
                    <td>{a.title}</td>
                    <td className="muted">{a.capability ? capLabel(a.capability) : '—'}</td>
                    <td><span className="badge badge-neutral">{a.affected_count}</span></td>
                    <td className="muted">{a.message}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
      <div className="stock-chart-controls">
        <div className="btn-group">
          <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
          <button className="btn btn-sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>下一页</button>
        </div>
        <span className="muted">第 {Math.floor(offset / PAGE) + 1} 页 · 共 {total} 条</span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ //
// F5-B Tab：维护建议
// ------------------------------------------------------------------ //
interface PrefillPayload {
  target: string;
  preset?: string;
  symbols?: string[];
  capabilities?: string[];
  allow_summary_only?: boolean;
}
function RecommendationsTab({ active, onPrefill }: {
  active: boolean;
  onPrefill: (r: WestockRecommendation) => void;
}) {
  const [offset, setOffset] = useState(0);
  const PAGE = 20;
  const q = useOps<{ total: number; limit: number; offset: number; items: WestockRecommendation[] }>(
    `recs-${offset}`, () => api.westockRecommendations({ limit: PAGE, offset }), active);
  const items = q.data?.data.items ?? [];
  const total = q.data?.data.total ?? 0;
  return (
    <div className="card section-card">
      <div className="card-title">维护建议</div>
      <p className="body-copy muted">建议由告警固定映射生成；“填入刷新表单”仅预填、需你再次确认提交，绝不自动执行。</p>
      {q.isLoading ? <div className="card loading-state">正在读取建议…</div>
        : q.isError ? <div className="alert alert-error">建议读取失败；不影响本地研究主链。</div>
          : items.length === 0 ? <OpsEmpty text="暂无维护建议。" /> : (
            <div className="table-wrap">
              <table className="table connection-table">
                <thead><tr><th>优先级</th><th>建议</th><th>原因</th><th>影响</th><th>操作</th></tr></thead>
                <tbody>{items.map((r) => (
                  <tr key={r.recommendation_id}>
                    <td><span className={`badge badge-${severityTone(r.priority)}`}>{SEVERITY_TEXT[r.priority] ?? r.priority}</span></td>
                    <td><strong>{REC_CODE_TEXT[r.code] ?? r.code}</strong></td>
                    <td className="muted">{r.reason}</td>
                    <td><span className="badge badge-neutral">{r.affected_count}</span></td>
                    <td>
                      {r.can_prefill_refresh
                        ? <button className="btn btn-sm" onClick={() => onPrefill(r)}>填入刷新表单</button>
                        : <span className="muted">
                          {r.target_kind === 'screener'
                            ? '通过原筛选结果重新导出'
                            : r.requires_workbuddy ? '需人工处理' : '仅检查说明'}
                        </span>}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
      <div className="stock-chart-controls">
        <div className="btn-group">
          <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
          <button className="btn btn-sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>下一页</button>
        </div>
        <span className="muted">第 {Math.floor(offset / PAGE) + 1} 页 · 共 {total} 条</span>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ //
// F5-B Tab：刷新趋势
// ------------------------------------------------------------------ //
function TrendsTab({ active }: { active: boolean }) {
  const [windowDays, setWindowDays] = useState<7 | 30>(7);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const q = useOps<WestockTrendsData>(
    `trends-${windowDays}`, () => api.westockTrends(windowDays), active);
  const d = q.data?.data;

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);
  useEffect(() => {
    const onResize = () => chartRef.current?.resize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    if (!active || !d || !containerRef.current) return;
    const hasData = d.daily.some((x) => x.requests_total > 0);
    if (!hasData) { chartRef.current?.dispose(); chartRef.current = null; return; }
    if (!chartRef.current) chartRef.current = echarts.init(containerRef.current);
    const dates = d.daily.map((x) => x.date.slice(5));
    const reqs = d.daily.map((x) => x.requests_total);
    const rates = d.daily.map((x) => (x.success_rate == null ? null : +(x.success_rate * 100).toFixed(1)));
    chartRef.current.setOption({
      grid: { left: 40, right: 40, top: 30, bottom: 30 },
      tooltip: { trigger: 'axis', renderMode: 'richText' },
      legend: { data: ['请求数', '成功率 %'] },
      xAxis: { type: 'category', data: dates },
      yAxis: [{ type: 'value', name: '请求' }, { type: 'value', name: '%', max: 100 }],
      series: [
        { name: '请求数', type: 'bar', data: reqs },
        { name: '成功率 %', type: 'line', yAxisIndex: 1, data: rates, smooth: true },
      ],
    });
  }, [d, active, windowDays]);

  const hasAny = d?.daily.some((x) => x.requests_total > 0) ?? false;
  return (
    <div className="card section-card">
      <div className="card-title">刷新趋势（只读）</div>
      <p className="body-copy muted">仅反映刷新请求/job 历史；系统未保存历史缓存快照，不展示缓存时效趋势。</p>
      <div className="refresh-form-row">
        <div className="btn-group">
          <button className={`btn btn-sm${windowDays === 7 ? ' active' : ''}`} onClick={() => setWindowDays(7)}>近 7 天</button>
          <button className={`btn btn-sm${windowDays === 30 ? ' active' : ''}`} onClick={() => setWindowDays(30)}>近 30 天</button>
        </div>
        {d && <span className="muted">{d.start_date} ~ {d.end_date}（上海时区）</span>}
      </div>
      {q.isLoading ? <div className="card loading-state">正在读取趋势…</div>
        : q.isError ? <div className="alert alert-error">趋势读取失败；不影响本地研究主链。</div>
          : !d ? <OpsEmpty text="暂无趋势数据。" />
            : !hasAny ? <OpsEmpty text="该窗口内暂无刷新历史。" />
              : (
                <div ref={containerRef} className="ops-chart" style={{ height: 280 }} aria-label="刷新趋势图" />
              )}
    </div>
  );
}

// ------------------------------------------------------------------ //
// Tab：连接状态（既有内容）
// ------------------------------------------------------------------ //
function ConnectionStatusTab({ prefill, onPrefillConsumed }: {
  prefill: PrefillPayload | null;
  onPrefillConsumed: () => void;
}) {
  const client = useQueryClient();
  const [notice, setNotice] = useState('');
  const [noticeTone, setNoticeTone] = useState<'success' | 'error'>('success');
  const [target, setTarget] = useState('stock');
  const [preset, setPreset] = useState('basic');
  const [symbolsText, setSymbolsText] = useState('');
  const [allowSummary, setAllowSummary] = useState(false);
  const [resultId, setResultId] = useState('');
  // 个股刷新两种互斥模式：preset（预设组合） / capabilities（指定能力）
  const [stockMode, setStockMode] = useState<'preset' | 'capabilities'>('preset');
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [offset, setOffset] = useState(0);
  const PAGE_SIZE = 5;

  // 建议预填：仅填充表单，绝不自动提交
  useEffect(() => {
    if (!prefill) return;
    if (prefill.target === 'stock' && (prefill.symbols?.length || prefill.capabilities?.length)) {
      setTarget('stock');
      // 能力来自注册表 → 去重 → 最多 20；命中即切到 capabilities 模式并勾选
      const caps = [...new Set(prefill.capabilities ?? [])]
        .filter((c) => STOCK_CAP_KEYS.includes(c))
        .slice(0, MAX_CAPABILITIES);
      if (caps.length > 0) {
        setStockMode('capabilities');
        setCapabilities(caps);
      } else if (prefill.preset) {
        setStockMode('preset');
        setPreset(prefill.preset);
      }
      if (prefill.symbols?.length) setSymbolsText(prefill.symbols.join(','));
      setAllowSummary(prefill.allow_summary_only === true);
      setNotice(caps.length > 0
        ? `已按维护建议预填：指定能力 ${caps.join('、')}；请确认后点击“创建刷新请求”提交。`
        : '已按维护建议预填刷新表单；请确认后提交。');
      setNoticeTone('success');
    } else if (prefill.target === 'market' && prefill.preset) {
      setTarget('market');
      setPreset(prefill.preset);
      setNotice('已按维护建议预填市场刷新表单；请确认后提交。');
      setNoticeTone('success');
    } else if (prefill.target === 'screener') {
      setTarget('screener');
      setNotice('选股类建议需通过原筛选结果重新导出；请填写 result_id。');
      setNoticeTone('success');
    }
    onPrefillConsumed();
  }, [prefill, onPrefillConsumed]);

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
      if (stockMode === 'capabilities') {
        // 后端要求 preset 与 capabilities 严格二选一：此模式绝不携带 preset
        const caps = [...new Set(capabilities)].filter((c) => STOCK_CAP_KEYS.includes(c));
        if (caps.length === 0 || caps.length > MAX_CAPABILITIES) {
          setNotice(`请选择 1–${MAX_CAPABILITIES} 个能力。`);
          setNoticeTone('error');
          return;
        }
        create.mutate({ target, capabilities: caps, symbols, allow_summary_only: allowSummary });
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
    <>
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
          {target === 'stock' && (
            <label className="deep-field-label">刷新方式
              <select
                value={stockMode}
                aria-label="个股刷新方式"
                onChange={(e) => setStockMode(e.target.value as 'preset' | 'capabilities')}
              >
                <option value="preset">按预设组合</option>
                <option value="capabilities">指定能力</option>
              </select>
            </label>
          )}
          {(target === 'market' || (target === 'stock' && stockMode === 'preset')) && (
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
        {target === 'stock' && stockMode === 'capabilities' && (
          <div className="refresh-capabilities" data-testid="stock-capabilities">
            <p className="body-copy muted">
              指定能力模式：请求只包含所选能力（不发送预设）；可自行增删，最多 {MAX_CAPABILITIES} 个。
              已选 {capabilities.length} 个。
            </p>
            <div className="refresh-form-row wrap">
              {STOCK_CAPABILITIES.map((c) => {
                const checked = capabilities.includes(c.key);
                return (
                  <label key={c.key} className="deep-field-label checkbox-inline">
                    <input
                      type="checkbox"
                      checked={checked}
                      aria-label={`能力 ${c.key}`}
                      onChange={(e) => setCapabilities((prev) => (
                        e.target.checked
                          ? (prev.includes(c.key) ? prev : [...prev, c.key].slice(0, MAX_CAPABILITIES))
                          : prev.filter((k) => k !== c.key)
                      ))}
                    />
                    {c.label}
                  </label>
                );
              })}
            </div>
          </div>
        )}
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
                          <span className="deep-chip">{capLabel(j.capability)}·{j.scope === 'global' ? 'global' : j.scope.slice(0, 8)}: {JOB_STATUS_TEXT[j.status] ?? j.status}</span>
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
              <thead><tr><th>股票</th><th>本地历史</th>{stockCaps.map((cap) => <th key={cap}>{capLabel(cap)}</th>)}</tr></thead>
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
            .map(([cap, s]) => `${capLabel(cap)}(${STATUS_TEXT[s] ?? s})`).join('、')}</div>
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
                    <td><strong>{capLabel(item.name)}</strong> <span className="muted">{item.name}</span></td>
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
    </>
  );
}

// ------------------------------------------------------------------ //
// 页面：Tab 容器
// ------------------------------------------------------------------ //
export function WestockConnectionPage() {
  const [activeTab, setActiveTab] = useState<TabKey>('connection');
  const [prefill, setPrefill] = useState<PrefillPayload | null>(null);
  const handlePrefill = (r: WestockRecommendation) => {
    const target = r.target_kind === 'market' ? 'market' : r.target_kind === 'screener' ? 'screener' : 'stock';
    setPrefill({
      target,
      preset: r.preset ?? undefined,
      symbols: r.symbols,
      capabilities: r.capabilities,
      allow_summary_only: r.allow_summary_only === true,
    });
    setActiveTab('connection');
  };
  return (
    <div>
      <PageHeader title="Westock 数据连接" description="腾讯自选股研究数据旁路 · 不写入回测与模拟账户主链" />
      <nav className="ops-tabs" role="tablist" aria-label="运营视图">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            role="tab"
            aria-selected={activeTab === tab.key}
            className={`ops-tab${activeTab === tab.key ? ' active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      {activeTab === 'connection' && <ConnectionStatusTab prefill={prefill} onPrefillConsumed={() => setPrefill(null)} />}
      {activeTab === 'caches' && <CachesTab active />}
      {activeTab === 'capabilities' && <CapabilitiesTab active />}
      {activeTab === 'symbols' && <SymbolsTab active />}
      {activeTab === 'history' && <HistoryTab active />}
      {activeTab === 'failures' && <FailuresTab active />}
      {activeTab === 'health' && <HealthTab active />}
      {activeTab === 'alerts' && <AlertsTab active />}
      {activeTab === 'recommendations' && <RecommendationsTab active onPrefill={handlePrefill} />}
      {activeTab === 'trends' && <TrendsTab active />}
    </div>
  );
}
