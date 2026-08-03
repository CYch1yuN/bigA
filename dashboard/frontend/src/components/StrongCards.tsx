import { useState } from 'react';
import type { CapabilityMeta, DeepCapabilityStatus } from '../api/client';

// ---------------------------------------------------------------------- //
// 通用格式化（永不输出 NaN / undefined / [object Object] / 原始 JSON）
// ---------------------------------------------------------------------- //

export function fmtNum(value: unknown): string {
  if (value == null) return '暂无';
  const n = Number(value);
  if (!Number.isFinite(n)) return '暂无';
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

export function fmtMoney(value: unknown): string {
  if (value == null) return '暂无';
  const n = Number(value);
  if (!Number.isFinite(n)) return '暂无';
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)} 亿元`;
  if (abs >= 1e4) return `${(n / 1e4).toFixed(2)} 万元`;
  return `${n.toFixed(2)} 元`;
}

export function fmtShares(value: unknown): string {
  if (value == null) return '暂无';
  const n = Number(value);
  if (!Number.isFinite(n)) return '暂无';
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)} 亿股`;
  if (abs >= 1e4) return `${(n / 1e4).toFixed(2)} 万股`;
  return `${n.toFixed(0)} 股`;
}

export function fmtRatio(value: unknown): string {
  if (value == null) return '暂无';
  const n = Number(value);
  if (!Number.isFinite(n)) return '暂无';
  return `${n.toFixed(2)}%`;
}

export function fmtText(value: unknown): string {
  if (value == null) return '暂无';
  const s = String(value);
  if (s === '[object Object]' || s === 'NaN' || s === 'undefined') return '暂无';
  return s;
}

const STATUS_TEXT: Record<DeepCapabilityStatus, string> = {
  fresh: '新鲜', stale: '已过期', unavailable: '无缓存',
};

/** 卡片元数据：数据日期 / 抓取时间 / 缓存年龄；不显示 capability 英文标识或 tool 名。 */
export function MetaLine({ meta }: { meta: CapabilityMeta | null | undefined }) {
  if (!meta) return null;
  const tone = meta.status === 'fresh' ? 'success' : meta.status === 'stale' ? 'warning' : 'neutral';
  const ageText = meta.cache_age_seconds == null ? '—' : `${meta.cache_age_seconds} 秒`;
  const fetched = meta.fetched_at ? meta.fetched_at.slice(0, 19).replace('T', ' ') : '—';
  return (
    <div className="card-meta muted">
      <span>数据日期 {meta.as_of ?? '—'}</span>
      <span>抓取 {fetched}</span>
      <span>缓存年龄 {ageText}</span>
      <span className={`badge badge-${tone}`}>{STATUS_TEXT[meta.status]}</span>
    </div>
  );
}

/** 长文本折叠（展开/收起）。 */
export function CollapsibleText({ text, limit = 120 }: { text: string; limit?: number }) {
  const [open, setOpen] = useState(false);
  if (text.length <= limit) return <span>{text}</span>;
  return (
    <span>
      {open ? text : `${text.slice(0, limit)}…`}
      <button className="btn btn-sm link-like" onClick={() => setOpen((v) => !v)}>
        {open ? '收起' : '展开'}
      </button>
    </span>
  );
}

/** 单卡片包装：标题 + 独立状态元数据 + 内容。 */
export function DeepCard({ title, meta, children }: {
  title: string; meta?: CapabilityMeta | null; children: React.ReactNode;
}) {
  return (
    <div className="deep-card">
      <div className="card-title compact">{title}</div>
      <MetaLine meta={meta} />
      {children}
    </div>
  );
}

/** 字段行：中文标签 + 值。 */
export function Field({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="deep-field"><span className="deep-field-label">{label}</span><span>{fmtText(value)}</span></div>
  );
}

/** 数值行：金额/比例/股数。 */
export function MoneyField({ label, value }: { label: string; value: unknown }) {
  return <Field label={label} value={value == null ? '暂无' : fmtMoney(value)} />;
}

export function RatioField({ label, value }: { label: string; value: unknown }) {
  return <Field label={label} value={value == null ? '暂无' : fmtRatio(value)} />;
}

export function SharesField({ label, value }: { label: string; value: unknown }) {
  return <Field label={label} value={value == null ? '暂无' : fmtShares(value)} />;
}

// ---------------------------------------------------------------------- //
// 各强类型卡片
// ---------------------------------------------------------------------- //

export function ProfileCard({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="公司资料" meta={meta}><p className="body-copy muted">暂无公司资料。</p></DeepCard>;
  return (
    <DeepCard title="公司资料" meta={meta}>
      <Field label="公司名称" value={data.name} />
      <Field label="所属行业" value={data.industry} />
      <Field label="主营业务" value={data.business} />
      <Field label="上市日期" value={data.list_date} />
      <Field label="注册资本" value={data.registered_capital} />
    </DeepCard>
  );
}

export function FinancialSummary({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="财务摘要" meta={meta}><p className="body-copy muted">暂无财务数据。</p></DeepCard>;
  const summary = data.summary as Record<string, unknown> | undefined;
  const sheet = (key: string) => (data[key] as Record<string, unknown> | undefined) ?? undefined;
  const income = sheet('income_statement');
  const balance = sheet('balance_sheet');
  const cashflow = sheet('cash_flow');
  return (
    <DeepCard title="财务摘要" meta={meta}>
      <Field label="报告期" value={summary?.report_date} />
      <MoneyField label="营业收入" value={summary?.revenue} />
      <MoneyField label="净利润" value={summary?.net_profit} />
      <RatioField label="加权净资产收益率" value={summary?.roe} />
      <Field label="每股收益" value={summary?.eps == null ? '暂无' : `${fmtNum(summary.eps)} 元`} />

      <div className="muted">利润表摘要</div>
      <MoneyField label="营业收入" value={income?.revenue} />
      <MoneyField label="营业成本" value={income?.cost} />
      <MoneyField label="营业利润" value={income?.operating_profit} />
      <MoneyField label="利润总额" value={income?.total_profit} />
      <MoneyField label="净利润" value={income?.net_profit} />

      <div className="muted">资产负债表摘要</div>
      <MoneyField label="总资产" value={balance?.total_assets} />
      <MoneyField label="总负债" value={balance?.total_liabilities} />
      <MoneyField label="股东权益" value={balance?.equity} />
      <MoneyField label="货币资金" value={balance?.cash} />
      <MoneyField label="应收账款" value={balance?.accounts_receivable} />

      <div className="muted">现金流量表摘要</div>
      <MoneyField label="经营现金流" value={cashflow?.operating_cash_flow} />
      <MoneyField label="投资现金流" value={cashflow?.investing_cash_flow} />
      <MoneyField label="融资现金流" value={cashflow?.financing_cash_flow} />
      <MoneyField label="现金净增加额" value={cashflow?.net_cash_flow} />
    </DeepCard>
  );
}

export function ForecastCard({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="机构预期与评级" meta={meta}><p className="body-copy muted">暂无机构预期。</p></DeepCard>;
  return (
    <DeepCard title="机构预期与评级" meta={meta}>
      <Field label="预测期间" value={data.report_date} />
      <Field label="一致预期每股收益" value={data.consensus_eps == null ? '暂无' : `${fmtNum(data.consensus_eps)} 元`} />
      <MoneyField label="一致预期营收" value={data.consensus_revenue} />
      <Field label="评级摘要" value={data.rating} />
      <Field label="目标价" value={data.target_price == null ? '暂无' : `${fmtNum(data.target_price)} 元`} />
    </DeepCard>
  );
}

export function ShareholderTable({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="股东与股本" meta={meta}><p className="body-copy muted">暂无股东数据。</p></DeepCard>;
  const holders = (data.major_shareholders ?? []) as Record<string, unknown>[];
  const structure = data.share_structure as Record<string, unknown> | undefined;
  return (
    <DeepCard title="股东与股本" meta={meta}>
      <Field label="股东人数" value={data.holder_count == null ? '暂无' : fmtNum(data.holder_count)} />
      <Field label="股东人数变化" value={data.holder_count_change == null ? '暂无' : fmtNum(data.holder_count_change)} />
      <div className="muted">股本结构</div>
      <SharesField label="总股本" value={structure?.total_shares} />
      <SharesField label="流通股本" value={structure?.float_shares} />
      <SharesField label="限售股本" value={structure?.restricted_shares} />
      <div className="muted">主要股东（前 20）</div>
      {holders.length === 0 ? <p className="body-copy muted">暂无主要股东。</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>股东名称</th><th>持股数</th><th>比例</th><th>变化</th></tr></thead>
            <tbody>
              {holders.map((h, i) => (
                <tr key={i}><td>{fmtText(h.name)}</td><td>{fmtShares(h.shares)}</td><td>{fmtRatio(h.ratio)}</td><td>{fmtNum(h.change)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeepCard>
  );
}

export function DividendCard({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="分红" meta={meta}><p className="body-copy muted">暂无分红数据。</p></DeepCard>;
  return (
    <DeepCard title="分红" meta={meta}>
      <Field label="分红方案" value={data.plan} />
      <Field label="除权日" value={data.ex_date} />
      <Field label="派息日" value={data.pay_date} />
    </DeepCard>
  );
}

export function BuybackCard({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="回购" meta={meta}><p className="body-copy muted">暂无回购数据。</p></DeepCard>;
  return (
    <DeepCard title="回购" meta={meta}>
      <Field label="回购状态" value={data.status} />
      <Field label="价格区间" value={data.price_range} />
      <MoneyField label="回购金额" value={data.amount} />
    </DeepCard>
  );
}

export function FundFlowCard({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="资金流" meta={meta}><p className="body-copy muted">暂无资金流数据。</p></DeepCard>;
  return (
    <DeepCard title="资金流（净流入）" meta={meta}>
      <MoneyField label="主力" value={data.main} />
      <MoneyField label="超大单" value={data.super_large} />
      <MoneyField label="大单" value={data.large} />
      <MoneyField label="中单" value={data.medium} />
      <MoneyField label="小单" value={data.small} />
    </DeepCard>
  );
}

export function MarginCard({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="融资融券" meta={meta}><p className="body-copy muted">暂无两融数据。</p></DeepCard>;
  return (
    <DeepCard title="融资融券" meta={meta}>
      <MoneyField label="融资余额" value={data.margin_balance} />
      <MoneyField label="融资变化" value={data.margin_change} />
      <MoneyField label="融券余额" value={data.short_balance} />
      <MoneyField label="融券变化" value={data.short_change} />
    </DeepCard>
  );
}

export function NorthboundCard({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="北向持股" meta={meta}><p className="body-copy muted">暂无北向持股数据。</p></DeepCard>;
  return (
    <DeepCard title="北向持股" meta={meta}>
      <SharesField label="持股数量" value={data.holding_shares} />
      <RatioField label="持股比例" value={data.holding_ratio} />
      <SharesField label="持股变化" value={data.change} />
    </DeepCard>
  );
}

export function BlockTradeTable({ data, meta }: { data: Record<string, unknown>[] | null | undefined; meta?: CapabilityMeta | null }) {
  return (
    <DeepCard title="大宗交易" meta={meta}>
      {!data || data.length === 0 ? <p className="body-copy muted">暂无大宗交易。</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>日期</th><th>价格</th><th>股数</th><th>金额</th><th>折价率</th></tr></thead>
            <tbody>
              {data.map((t, i) => (
                <tr key={i}><td>{fmtText(t.date)}</td><td>{fmtNum(t.price)} 元</td><td>{fmtShares(t.shares)}</td>
                  <td>{fmtMoney(t.amount)}</td><td>{t.discount == null ? '暂无' : `${fmtNum(t.discount)}%`}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeepCard>
  );
}

export function LhbTable({ data, meta }: { data: Record<string, unknown>[] | null | undefined; meta?: CapabilityMeta | null }) {
  return (
    <DeepCard title="龙虎榜" meta={meta}>
      {!data || data.length === 0 ? <p className="body-copy muted">暂无龙虎榜数据。</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>日期</th><th>上榜原因</th><th>席位</th><th>净买入</th><th>买入</th><th>卖出</th></tr></thead>
            <tbody>
              {data.map((t, i) => (
                <tr key={i}><td>{fmtText(t.date)}</td><td>{fmtText(t.reason)}</td><td>{fmtText(t.seat)}</td>
                  <td>{fmtMoney(t.net_buy)}</td><td>{fmtMoney(t.buy)}</td><td>{fmtMoney(t.sell)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeepCard>
  );
}

export function ChipSummary({ data, meta }: { data: Record<string, unknown> | null | undefined; meta?: CapabilityMeta | null }) {
  if (!data) return <DeepCard title="筹码分布" meta={meta}><p className="body-copy muted">暂无筹码数据。</p></DeepCard>;
  const dist = (data.distribution ?? []) as Record<string, unknown>[];
  return (
    <DeepCard title="筹码分布" meta={meta}>
      <Field label="筹码集中度" value={data.concentration == null ? '暂无' : fmtRatio(data.concentration)} />
      <div className="muted">价格分布（前 50 点）</div>
      {dist.length === 0 ? <p className="body-copy muted">暂无分布点。</p> : (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>价格</th><th>比例</th><th>筹码量</th></tr></thead>
            <tbody>
              {dist.map((p, i) => (
                <tr key={i}><td>{fmtNum(p.price)} 元</td><td>{fmtRatio(p.ratio)}</td><td>{fmtNum(p.chips)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeepCard>
  );
}

export function IntelList({ items, category, metas }: {
  items: { category: string; title?: string; summary?: string; source?: string; date?: string; url?: string; org?: string; rating?: string; target_price?: number; ann_type?: string }[];
  category: string;
  metas?: Record<string, CapabilityMeta | null>;
}) {
  const label = (c: string) => ({ news: '新闻', reports: '研报', announcements: '公告' })[c] ?? c;
  const groups = category ? [category] : ['news', 'reports', 'announcements'];
  return (
    <DeepCard title="资讯 · 研报 · 公告">
      <div className="deep-status-row">
        {groups.map((c) => (
          <span key={c} className="deep-status-item">
            <span className="badge badge-neutral">{label(c)}</span>
            <MetaLine meta={metas?.[c] ?? null} />
          </span>
        ))}
      </div>
      {items.length === 0 ? <p className="body-copy muted">暂无资讯条目。</p> : (
        <ul className="stock-list">
          {items.map((item, i) => (
            <li key={i}>
              <span className="badge badge-neutral">{label(item.category)}</span>
              {' '}{item.date ?? '日期未知'} · <strong>{fmtText(item.title)}</strong>
              {item.source ? `（${item.source}）` : ''}
              {item.org ? ` · ${item.org}${item.rating ? ` ${item.rating}` : ''}${item.target_price != null ? ` 目标价 ${fmtNum(item.target_price)} 元` : ''}` : ''}
              {item.summary ? <div className="muted"><CollapsibleText text={fmtText(item.summary)} /></div> : null}
              {item.url ? <div><a href={item.url} target="_blank" rel="noopener noreferrer">查看原文</a></div> : null}
            </li>
          ))}
        </ul>
      )}
    </DeepCard>
  );
}

export function EventsList({ data, meta }: { data: { date?: string; type?: string; title?: string; summary?: string; tags?: string[] }[] | null | undefined; meta?: CapabilityMeta | null }) {
  return (
    <DeepCard title="事件" meta={meta}>
      {!data || data.length === 0 ? <p className="body-copy muted">暂无事件。</p> : (
        <ul className="stock-list">
          {data.map((e, i) => (
            <li key={i}>
              {e.date ?? '日期未知'} · {e.type ? <span className="badge badge-neutral">{e.type}</span> : null}
              {' '}<strong>{fmtText(e.title)}</strong>
              {e.tags?.length ? ` [${e.tags.join('、')}]` : ''}
              {e.summary ? <div className="muted"><CollapsibleText text={fmtText(e.summary)} /></div> : null}
            </li>
          ))}
        </ul>
      )}
    </DeepCard>
  );
}

export function RiskList({ data, meta }: { data: { severity?: string; title?: string; description?: string }[] | null | undefined; meta?: CapabilityMeta | null }) {
  return (
    <DeepCard title="风险提示（Westock 来源，不替代人工判断）" meta={meta}>
      {!data || data.length === 0 ? <p className="body-copy muted">暂无风险提示。</p> : (
        <ul className="stock-list">
          {data.map((r, i) => (
            <li key={i}>
              {r.severity ? <span className="badge badge-warning">{fmtText(r.severity)}</span> : null}
              {' '}<strong>{fmtText(r.title)}</strong>
              {r.description ? <div className="muted"><CollapsibleText text={fmtText(r.description)} /></div> : null}
            </li>
          ))}
        </ul>
      )}
    </DeepCard>
  );
}

const TECH_SAFETY_NOTE = '技术指标来自 Westock 缓存，仅作研究展示；BigA 策略与回测使用本地 curated 数据独立计算。';

export function TechnicalPanel({ data, meta, note }: {
  data: Record<string, unknown> | null | undefined;
  meta?: CapabilityMeta | null;
  note?: string;
}) {
  if (!data) return <DeepCard title="技术指标" meta={meta}><p className="body-copy muted">暂无技术指标。</p></DeepCard>;
  const group = (key: string) => (data[key] as Record<string, unknown> | undefined) ?? null;
  const renderGroup = (title: string, g: Record<string, unknown> | null) => (
    <div>
      <div className="muted">{title}</div>
      {g ? (
        <div className="deep-fields-inline">
          {Object.entries(g).map(([k, v]) => <span key={k} className="deep-chip">{k}: {fmtNum(v)}</span>)}
        </div>
      ) : <p className="body-copy muted">暂无</p>}
    </div>
  );
  return (
    <DeepCard title="技术指标（Westock 缓存，仅展示）" meta={meta}>
      <Field label="指标日期" value={data.date} />
      {renderGroup('MA（5/10/20/60）', group('ma'))}
      {renderGroup('MACD', group('macd'))}
      {renderGroup('KDJ', group('kdj'))}
      {renderGroup('BOLL', group('boll'))}
      {typeof data.rsi === 'number' ? renderGroup('RSI', { rsi: data.rsi }) : renderGroup('RSI（6/12/24）', group('rsi'))}
      <p className="body-copy muted">{fmtText(note) !== '暂无' ? fmtText(note) : TECH_SAFETY_NOTE}</p>
    </DeepCard>
  );
}
