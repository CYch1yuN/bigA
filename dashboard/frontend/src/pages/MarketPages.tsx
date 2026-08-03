import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  api,
  CapabilityMeta,
  MarketCalendarResponse,
  MarketDistributionResponse,
  MarketEventsResponse,
  MarketFundsResponse,
  MarketHotResponse,
  MarketMacroResponse,
  MarketOverviewResponse,
  MarketSectorsResponse,
  MarketIndexesResponse,
  MarketConstituentsResponse,
  MarketIndustryChainResponse,
} from '../api/client';
import { MetaLine, fmtMoney, fmtNum, fmtText, CollapsibleText } from '../components/StrongCards';
import {
  ChangeDistributionChart, MarketDimensionsChart, SectorBarChart, MarketFundsBar,
} from '../components/MarketCharts';
import { PageHeader } from '../components/ui';

const STALE_TIME = 5 * 60 * 1000;
const MARKET_DISCLAIMER = '市场数据来自 Westock 缓存，仅作研究背景，不直接生成 BigA 信号、订单或持仓，也不修改 Gate 4B。';

function MarketSection({ title, loading, error, meta, warnings, children }: {
  title: string;
  loading: boolean;
  error: boolean;
  meta?: Record<string, CapabilityMeta | null>;
  warnings?: string[];
  children: React.ReactNode;
}) {
  if (loading) return <div className="card section-card"><div className="card-title compact">{title}</div><div className="loading-state">正在读取 Westock 缓存…</div></div>;
  if (error) return <div className="card section-card"><div className="card-title compact">{title}</div><div className="alert alert-error">无法读取该区域；不影响其他区域。</div></div>;
  return (
    <div className="card section-card">
      <div className="card-title compact">{title}</div>
      <div className="muted">{MARKET_DISCLAIMER}</div>
      {children}
      {meta ? Object.entries(meta).map(([cap, m]) => <MetaLine key={cap} meta={m} />) : null}
      {warnings?.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// 市场概览（默认 Tab）：overview + distribution
// ---------------------------------------------------------------------- //

export function OverviewTab() {
  const overview = useQuery({ queryKey: ['market-overview'], queryFn: () => api.marketOverview(), staleTime: STALE_TIME });
  const dist = useQuery({ queryKey: ['market-distribution'], queryFn: () => api.marketDistribution(), staleTime: STALE_TIME });
  const o = overview.data as MarketOverviewResponse | undefined;
  const d = dist.data as MarketDistributionResponse | undefined;
  const ov = o?.data.overview;
  const distData = d?.data.distribution;
  const meta = { market_overview: o?.capability_meta.market_overview ?? null, change_distribution: d?.capability_meta.change_distribution ?? null };
  const warnings = [...(o?.warnings ?? []), ...(d?.warnings ?? [])];
  return (
    <div className="card section-card">
      <div className="card-title compact">市场概览</div>
      <div className="muted">{MARKET_DISCLAIMER}</div>

      {/* 子区域一：市场评分（独立降级，不影响涨跌分布） */}
      {overview.isLoading ? <div className="loading-state">正在读取 Westock 缓存…</div>
        : overview.isError ? <div className="alert alert-error">无法读取市场评分。</div>
        : !ov ? <p className="body-copy muted">暂无市场评分缓存。</p>
        : (
          <div className="deep-grid">
            <div className="deep-card">
              <div className="card-title compact">市场评分</div>
              <div className="metric-value" style={{ color: 'var(--color-primary)' }}>{ov.score != null ? fmtNum(ov.score) : '暂无'}</div>
              <div className="muted">情绪 {ov.sentiment != null ? fmtNum(ov.sentiment) : '暂无'} · 波动 {ov.volatility != null ? fmtNum(ov.volatility) : '暂无'}</div>
              <div className="muted">风险等级：{ov.risk_level ?? '暂无'}</div>
              {ov.summary ? <div className="muted"><CollapsibleText text={fmtText(ov.summary)} /></div> : null}
              <MetaLine meta={meta.market_overview} />
            </div>
            {ov.dimensions ? <MarketDimensionsChart dimensions={ov.dimensions} /> : null}
          </div>
        )}

      {/* 子区域二：涨跌分布（独立降级，不影响市场评分） */}
      <div className="muted">涨跌家数</div>
      {dist.isLoading ? <div className="loading-state">正在读取 Westock 缓存…</div>
        : dist.isError ? <div className="alert alert-error">无法读取涨跌分布。</div>
        : !distData ? <p className="body-copy muted">暂无涨跌分布缓存。</p>
        : (
          <>
            <div className="deep-fields-inline">
              <span className="deep-chip">上涨 {fmtNum(distData.rise_count)} 家</span>
              <span className="deep-chip">下跌 {fmtNum(distData.fall_count)} 家</span>
              <span className="deep-chip">平盘 {fmtNum(distData.flat_count)} 家</span>
              <span className="deep-chip">涨停 {fmtNum(distData.limit_up_count)} 家</span>
              <span className="deep-chip">跌停 {fmtNum(distData.limit_down_count)} 家</span>
              <span className="deep-chip">成交 {fmtMoney(distData.total_amount)}</span>
            </div>
            <MetaLine meta={meta.change_distribution} />
            {distData.bins ? <ChangeDistributionChart bins={distData.bins} /> : null}
          </>
        )}
      {warnings.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// 热点排行 Tab
// ---------------------------------------------------------------------- //

export function HotTab() {
  const q = useQuery({ queryKey: ['market-hot'], queryFn: () => api.marketHot(), staleTime: STALE_TIME });
  const d = q.data as MarketHotResponse | undefined;
  const hot = d?.data.hot;
  return (
    <MarketSection title="热点排行" loading={q.isLoading} error={q.isError}
      meta={d?.capability_meta} warnings={d?.warnings}>
      <div className="stock-groups">
        <div>
          <div className="muted">热门股票（前 100）</div>
          {!hot?.stocks?.length ? <p className="body-copy muted">暂无热门股票。</p> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>排名</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>热度</th></tr></thead>
                <tbody>
                  {hot.stocks.map((s, i) => (
                    <tr key={i}>
                      <td>{fmtNum(s.rank)}</td>
                      <td>{s.symbol && s.local_history_available
                        ? <Link to={`/stocks/${encodeURIComponent(s.symbol)}`}>{s.symbol}</Link>
                        : <span>{s.symbol ?? '暂无'}{s.symbol && !s.local_history_available ? '（尚未补跑）' : ''}</span>}</td>
                      <td>{fmtText(s.name)}</td>
                      <td>{s.price != null ? `${fmtNum(s.price)} 元` : '暂无'}</td>
                      <td style={{ color: Number(s.change_percent ?? 0) >= 0 ? 'var(--color-danger)' : 'var(--color-success)' }}>
                        {s.change_percent != null ? `${fmtNum(s.change_percent)}%` : '暂无'}</td>
                      <td>{fmtNum(s.heat)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div>
          <div className="muted">热门板块（前 50）</div>
          {!hot?.sectors?.length ? <p className="body-copy muted">暂无热门板块。</p> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>排名</th><th>板块</th><th>涨跌幅</th><th>热度</th><th>领涨</th></tr></thead>
                <tbody>
                  {hot.sectors.map((s, i) => (
                    <tr key={i}>
                      <td>{fmtNum(s.rank)}</td>
                      <td>{fmtText(s.name)}</td>
                      <td style={{ color: Number(s.change_percent ?? 0) >= 0 ? 'var(--color-danger)' : 'var(--color-success)' }}>
                        {s.change_percent != null ? `${fmtNum(s.change_percent)}%` : '暂无'}</td>
                      <td>{fmtNum(s.heat)}</td>
                      <td>{s.leader_symbol && s.leader_local_history_available
                        ? <Link to={`/stocks/${encodeURIComponent(s.leader_symbol)}`}>{fmtText(s.leader_name) || s.leader_symbol}</Link>
                        : <span>{fmtText(s.leader_name) || s.leader_symbol || '暂无'}{s.leader_symbol && !s.leader_local_history_available ? '（尚未补跑）' : ''}</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </MarketSection>
  );
}

// ---------------------------------------------------------------------- //
// 资金 Tab
// ---------------------------------------------------------------------- //

export function FundsTab() {
  const q = useQuery({ queryKey: ['market-funds'], queryFn: () => api.marketFunds(), staleTime: STALE_TIME });
  const d = q.data as MarketFundsResponse | undefined;
  const funds = d?.data.funds;
  return (
    <MarketSection title="市场资金" loading={q.isLoading} error={q.isError}
      meta={d?.capability_meta} warnings={d?.warnings}>
      {funds && (funds.margin_balance != null || funds.northbound_net != null || funds.northbound_holding != null) ? (
        <>
          <div className="deep-fields-inline">
            <span className="deep-chip">融资余额 {fmtMoney(funds.margin_balance)}</span>
            <span className="deep-chip">融资变化 {fmtMoney(funds.margin_change)}</span>
            <span className="deep-chip">北向净流入 {fmtMoney(funds.northbound_net)}</span>
            <span className="deep-chip">北向持股 {fmtSharesText(funds.northbound_holding)}</span>
            <span className="deep-chip">南向净流入 {funds.southbound_net == null ? '暂无' : fmtMoney(funds.southbound_net)}</span>
            <span className="deep-chip">数据日期 {funds.date ?? '暂无'}</span>
          </div>
          <MarketFundsBar funds={funds} />
        </>
      ) : <p className="body-copy muted">暂无市场资金数据。</p>}
    </MarketSection>
  );
}

function fmtSharesText(v: number | null | undefined): string {
  if (v == null) return '暂无';
  const n = Number(v);
  if (!Number.isFinite(n)) return '暂无';
  if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(2)} 亿股`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(2)} 万股`;
  return `${n.toFixed(0)} 股`;
}

// ---------------------------------------------------------------------- //
// 宏观日历 Tab
// ---------------------------------------------------------------------- //

export function MacroCalendarTab() {
  const macro = useQuery({ queryKey: ['market-macro'], queryFn: () => api.marketMacro(), staleTime: STALE_TIME });
  const calendar = useQuery({ queryKey: ['market-calendar'], queryFn: () => api.marketCalendar({ limit: 50, offset: 0 }), staleTime: STALE_TIME });
  const m = macro.data as MarketMacroResponse | undefined;
  const c = calendar.data as MarketCalendarResponse | undefined;
  const importanceText = (v?: string) => ({ low: '低', medium: '中', high: '高' })[v ?? ''] ?? '暂无';
  const warnings = [...(m?.warnings ?? []), ...(c?.warnings ?? [])];
  return (
    <div className="card section-card">
      <div className="card-title compact">宏观日历</div>
      <div className="muted">{MARKET_DISCLAIMER}</div>

      {/* 子区域一：宏观指标（独立降级，不影响日历） */}
      {macro.isLoading ? <div className="loading-state">正在读取 Westock 缓存…</div>
        : macro.isError ? <div className="alert alert-error">无法读取宏观指标。</div>
        : !m?.data.indicators?.length ? <p className="body-copy muted">暂无宏观指标缓存。</p>
        : (
          <>
            <div className="muted">宏观指标（前 200）</div>
            <div className="table-wrap">
              <table className="table market-macro-table">
                <thead><tr><th>指标</th><th>数值</th><th>单位</th><th>期间</th><th>重要性</th><th>前值</th><th>预测</th></tr></thead>
                <tbody>
                  {m.data.indicators.map((i, idx) => (
                    <tr key={idx}>
                      <td>{fmtText(i.name)}</td><td>{fmtNum(i.value)}</td><td>{i.unit ?? '暂无'}</td>
                      <td>{i.period ?? '暂无'}</td><td>{importanceText(i.importance)}</td>
                      <td>{i.previous != null ? fmtNum(i.previous) : '暂无'}</td>
                      <td>{i.forecast != null ? fmtNum(i.forecast) : '暂无'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {m.capability_meta.macro ? <MetaLine meta={m.capability_meta.macro} /> : null}
          </>
        )}

      {/* 子区域二：财经日历（独立降级，不影响宏观） */}
      <div className="muted">财经日历（前 500）</div>
      <div className="muted">财经日历由 Westock 事件与公告缓存派生，并非独立财经日历能力；actual、forecast、previous 仅在来源数据明确提供时展示。</div>
      {calendar.isLoading ? <div className="loading-state">正在读取 Westock 缓存…</div>
        : calendar.isError ? <div className="alert alert-error">无法读取财经日历。</div>
        : !c?.data.items?.length ? <p className="body-copy muted">暂无日历条目缓存。</p>
        : (
          <>
            <div className="table-wrap">
              <table className="table market-calendar-table">
                <thead>
                  <tr>
                    <th>日期</th><th>时间</th><th>国家/地区</th><th>重要性</th>
                    <th>事件</th><th>实际值</th><th>预测值</th><th>前值</th><th>原文</th>
                  </tr>
                </thead>
                <tbody>
                  {c.data.items.map((item, i) => (
                    <tr key={i}>
                      <td>{item.date || '暂无'}</td>
                      <td>{item.time || '暂无'}</td>
                      <td>{item.country || '暂无'}</td>
                      <td>{importanceText(item.importance)}</td>
                      <td>{fmtText(item.title)}</td>
                      <td>{item.actual != null ? fmtNum(item.actual) : '暂无'}</td>
                      <td>{item.forecast != null ? fmtNum(item.forecast) : '暂无'}</td>
                      <td>{item.previous != null ? fmtNum(item.previous) : '暂无'}</td>
                      <td>{item.url
                        ? <a href={item.url} target="_blank" rel="noopener noreferrer">查看原文</a>
                        : <span className="muted">暂无</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 日历来源元数据：中文来源名 + 独立 capability_meta（不显示英文/tool 名） */}
            <div className="deep-source-row">
              <div className="deep-source-block">
                <span className="badge badge-neutral">事件来源</span>
                {c.capability_meta.events ? <MetaLine meta={c.capability_meta.events} /> : <span className="muted">暂无缓存元数据</span>}
              </div>
              <div className="deep-source-block">
                <span className="badge badge-neutral">公告来源</span>
                {c.capability_meta.announcements ? <MetaLine meta={c.capability_meta.announcements} /> : <span className="muted">暂无缓存元数据</span>}
              </div>
            </div>
          </>
        )}
      {warnings.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// 市场事件 Tab
// ---------------------------------------------------------------------- //

export function MarketEventsTab() {
  const q = useQuery({ queryKey: ['market-events'], queryFn: () => api.marketEvents(), staleTime: STALE_TIME });
  const d = q.data as MarketEventsResponse | undefined;
  const catText = (c?: string) => ({ events: '事件', announcements: '公告', risk: '风险' })[c ?? ''] ?? c ?? '其他';
  return (
    <MarketSection title="市场事件" loading={q.isLoading} error={q.isError}
      meta={d?.capability_meta} warnings={d?.warnings}>
      {!d?.data.events?.length ? <p className="body-copy muted">暂无市场事件。</p> : (
        <ul className="stock-list">
          {d.data.events.map((e, i) => (
            <li key={i}>
              <span className="badge badge-neutral">{catText(e.category)}</span>
              {' '}{e.date ?? '日期未知'} · {e.severity ? <span className="badge badge-warning">{fmtText(e.severity)}</span> : null}
              {' '}<strong>{fmtText(e.title)}</strong>
              {e.symbols?.length ? <span> [{(e.symbols ?? []).slice(0, 5).join('、')}]</span> : null}
              {e.summary ? <div className="muted"><CollapsibleText text={fmtText(e.summary)} /></div> : null}
              {e.url ? <div><a href={e.url} target="_blank" rel="noopener noreferrer">查看原文</a></div> : null}
            </li>
          ))}
        </ul>
      )}
    </MarketSection>
  );
}

// ---------------------------------------------------------------------- //
// 产业链 Tab（点击后才请求）
// ---------------------------------------------------------------------- //

export function IndustryChainTab() {
  const q = useQuery({ queryKey: ['market-industry-chain'], queryFn: () => api.marketIndustryChain(), staleTime: STALE_TIME });
  const d = q.data as MarketIndustryChainResponse | undefined;
  const chains = d?.data.chains;
  const stageText: Record<string, string> = { upstream: '上游', midstream: '中游', downstream: '下游' };
  return (
    <MarketSection title="产业链" loading={q.isLoading} error={q.isError}
      meta={d?.capability_meta} warnings={d?.warnings}>
      {!chains?.length ? <p className="body-copy muted">暂无产业链数据。</p> : (
        chains.map((chain, ci) => (
          <div key={ci} className="deep-card" style={{ marginBottom: 12 }}>
            <div className="card-title compact">{fmtText(chain.name) || fmtText(chain.code)}</div>
            {chain.description ? <div className="muted"><CollapsibleText text={fmtText(chain.description)} /></div> : null}
            <div className="stock-groups">
              {(['upstream', 'midstream', 'downstream'] as const).map((stage) => (
                <div key={stage}>
                  <div className="muted">{stageText[stage]}</div>
                  {!chain[stage]?.length ? <p className="body-copy muted">暂无节点。</p> : (
                    <ul className="stock-list">
                      {chain[stage]!.map((node, ni) => (
                        <li key={ni}>
                          <strong>{fmtText(node.name)}</strong>
                          {node.node_type ? `（${fmtText(node.node_type)}）` : ''}
                          {/* related_symbols 仅显示受控代码文本，不创建个股链接 */}
                          {node.related_symbols?.length
                            ? <span> · 关联：{node.related_symbols.join('、')}</span>
                            : null}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))
      )}
    </MarketSection>
  );
}

// ---------------------------------------------------------------------- //
// /market 页面：6 Tab 懒加载
// ---------------------------------------------------------------------- //

export function MarketOverviewPage() {
  const [tab, setTab] = useState('overview');
  const TABS = [
    { key: 'overview', label: '市场概览', render: () => <OverviewTab /> },
    { key: 'hot', label: '热点排行', render: () => <HotTab /> },
    { key: 'funds', label: '资金', render: () => <FundsTab /> },
    { key: 'macro', label: '宏观日历', render: () => <MacroCalendarTab /> },
    { key: 'chain', label: '产业链', render: () => <IndustryChainTab /> },
    { key: 'events', label: '市场事件', render: () => <MarketEventsTab /> },
  ];
  return (
    <div>
      <PageHeader title="市场研究中心" description="Westock 缓存导出 · 非实时 · 仅研究展示" />
      <div className="btn-group page-tabs">
        {TABS.map((t) => (
          <button key={t.key} className={`btn btn-sm${tab === t.key ? ' btn-active' : ''}`} onClick={() => setTab(t.key)}>{t.label}</button>
        ))}
      </div>
      {TABS.map((t) => (tab === t.key ? <div key={t.key}>{t.render()}</div> : null))}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// /market/sectors
// ---------------------------------------------------------------------- //

export function MarketSectorsPage() {
  const [sectorType, setSectorType] = useState('all');
  const q = useQuery({ queryKey: ['market-sectors'], queryFn: () => api.marketSectors(), staleTime: STALE_TIME });
  const d = q.data as MarketSectorsResponse | undefined;
  const rows = (d?.data.sectors ?? []).filter((s) => sectorType === 'all' || s.sector_type === sectorType);
  const sorted = [...rows].sort((a, b) => (b.change_percent ?? 0) - (a.change_percent ?? 0));
  return (
    <div>
      <PageHeader title="板块表现" description="Westock 缓存导出 · 非实时 · 仅研究展示" />
      <div className="card section-card">
        <div className="card-title compact">板块</div>
        <div className="btn-group">
          <button className={`btn btn-sm${sectorType === 'all' ? ' btn-active' : ''}`} onClick={() => setSectorType('all')}>全部</button>
          <button className={`btn btn-sm${sectorType === 'industry' ? ' btn-active' : ''}`} onClick={() => setSectorType('industry')}>行业</button>
          <button className={`btn btn-sm${sectorType === 'concept' ? ' btn-active' : ''}`} onClick={() => setSectorType('concept')}>概念</button>
        </div>
        {q.isLoading && <div className="loading-state">正在读取 Westock 缓存…</div>}
        {q.isError && <div className="alert alert-error">无法读取板块数据。</div>}
        {!q.isLoading && !q.isError && (sorted.length ? (
          <>
            <SectorBarChart sectors={sorted} />
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>代码</th><th>名称</th><th>类型</th><th>涨跌幅</th><th>成交额</th><th>换手率</th><th>上涨/下跌</th><th>领涨</th></tr></thead>
                <tbody>
                  {sorted.map((s, i) => (
                    <tr key={i}>
                      <td>{fmtText(s.code)}</td><td>{fmtText(s.name)}</td>
                      <td>{s.sector_type === 'industry' ? '行业' : s.sector_type === 'concept' ? '概念' : '暂无'}</td>
                      <td style={{ color: Number(s.change_percent ?? 0) >= 0 ? 'var(--color-danger)' : 'var(--color-success)' }}>{s.change_percent != null ? `${fmtNum(s.change_percent)}%` : '暂无'}</td>
                      <td>{fmtMoney(s.amount)}</td>
                      <td>{s.turnover_rate != null ? `${fmtNum(s.turnover_rate)}%` : '暂无'}</td>
                      <td>{s.rise_count != null ? `${fmtNum(s.rise_count)}/${s.fall_count != null ? fmtNum(s.fall_count) : '暂无'}` : '暂无'}</td>
                      <td>{s.leader_symbol ? <Link to={`/stocks/${encodeURIComponent(s.leader_symbol)}`}>{fmtText(s.leader_name) || s.leader_symbol}</Link> : '暂无'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : <p className="body-copy muted">暂无板块数据。</p>)}
        <div className="muted">{MARKET_DISCLAIMER}</div>
        {d?.capability_meta.sector ? <MetaLine meta={d.capability_meta.sector} /> : null}
        {d?.warnings.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------- //
// /market/indexes
// ---------------------------------------------------------------------- //

export function MarketIndexesPage() {
  const q = useQuery({ queryKey: ['market-indexes'], queryFn: () => api.marketIndexes(), staleTime: STALE_TIME });
  const d = q.data as MarketIndexesResponse | undefined;
  const [selected, setSelected] = useState('');
  const cons = useQuery({
    queryKey: ['market-constituents', selected],
    queryFn: () => api.marketConstituents(selected),
    enabled: !!selected,
    staleTime: STALE_TIME,
  });
  const c = cons.data as MarketConstituentsResponse | undefined;
  return (
    <div>
      <PageHeader title="指数研究" description="Westock 缓存导出 · 非实时 · 仅研究展示" />
      <div className="card section-card">
        <div className="card-title compact">指数列表</div>
        {q.isLoading && <div className="loading-state">正在读取 Westock 缓存…</div>}
        {q.isError && <div className="alert alert-error">无法读取指数数据。</div>}
        {!q.isLoading && !q.isError && (d?.data.indexes?.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>代码</th><th>名称</th><th>点位</th><th>涨跌</th><th>涨跌幅</th><th /></tr></thead>
              <tbody>
                {d.data.indexes.map((ix, i) => (
                  <tr key={i}>
                    <td>{fmtText(ix.code)}</td><td>{fmtText(ix.name)}</td>
                    <td>{ix.price != null ? fmtNum(ix.price) : '暂无'}</td>
                    <td>{ix.change != null ? fmtNum(ix.change) : '暂无'}</td>
                    <td style={{ color: Number(ix.change_percent ?? 0) >= 0 ? 'var(--color-danger)' : 'var(--color-success)' }}>
                      {ix.change_percent != null ? `${fmtNum(ix.change_percent)}%` : '暂无'}</td>
                    <td><button className="btn btn-sm" onClick={() => setSelected(ix.code ?? '')}>成分股</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <p className="body-copy muted">暂无指数数据。</p>)}
        <div className="muted">{MARKET_DISCLAIMER}</div>
        {d?.capability_meta.index ? <MetaLine meta={d.capability_meta.index} /> : null}
        {d?.warnings.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
      </div>

      {selected && (
        <div className="card section-card">
          <div className="card-title compact">成分股（{selected}，前 1000）</div>
          {cons.isLoading && <div className="loading-state">正在读取成分股…</div>}
          {cons.isError && <div className="alert alert-error">无法读取成分股。</div>}
          {!cons.isLoading && !cons.isError && (c?.data.constituents?.length ? (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>代码</th><th>名称</th><th>权重</th><th>行业</th><th>本地 K 线</th><th /></tr></thead>
                <tbody>
                  {c.data.constituents.map((item, i) => (
                    <tr key={i}>
                      <td>{fmtText(item.symbol)}</td><td>{fmtText(item.name)}</td>
                      <td>{item.weight != null ? `${fmtNum(item.weight)}%` : '暂无'}</td>
                      <td>{item.industry ?? '暂无'}</td>
                      <td>{item.local_history_available
                        ? <span className="badge badge-success">本地数据可用</span>
                        : <span className="badge badge-neutral">尚未补跑</span>}</td>
                      <td>{item.local_history_available && item.symbol
                        ? <Link className="btn btn-sm" to={`/stocks/${encodeURIComponent(item.symbol)}`}>查看</Link>
                        : <span className="muted">无详情</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="body-copy muted">暂无成分股数据。</p>)}
          {c?.warnings.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
        </div>
      )}
    </div>
  );
}
