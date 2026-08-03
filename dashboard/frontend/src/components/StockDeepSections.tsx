import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import {
  api,
  StockEventsResponse,
  StockFundamentalsResponse,
  StockFundsResponse,
  StockIntelResponse,
  StockOwnershipResponse,
  StockTechnicalResponse,
} from '../api/client';
import {
  BlockTradeTable, BuybackCard, ChipSummary, DividendCard, EventsList,
  FinancialSummary, ForecastCard, FundFlowCard, IntelList, LhbTable,
  MarginCard, NorthboundCard, ProfileCard, RiskList, ShareholderTable, TechnicalPanel,
} from './StrongCards';

const STALE_TIME = 5 * 60 * 1000; // 5 分钟

// 资讯筛选：用户可见中文标签；内部 category 参数保持英文（API 协议不变）
const INTEL_FILTERS = [
  { key: 'news', label: '新闻' },
  { key: 'reports', label: '研报' },
  { key: 'announcements', label: '公告' },
];

const STATUS_TEXT = { fresh: '新鲜', stale: '已过期', unavailable: '无缓存' } as const;

function StatusRow({ availability }: { availability: Record<string, string> }) {
  return (
    <div className="deep-status-row">
      {Object.entries(availability).map(([capability, status]) => (
        <span key={capability} className="deep-status-item muted">
          {STATUS_TEXT[status as keyof typeof STATUS_TEXT] ?? status}
        </span>
      ))}
    </div>
  );
}

function SectionWrapper({ title, loading, error, availability, warnings, children }: {
  title: string;
  loading: boolean;
  error: boolean;
  availability?: Record<string, string>;
  warnings?: string[];
  children: React.ReactNode;
}) {
  if (loading) return <div className="card section-card"><div className="card-title compact">{title}</div><div className="loading-state">正在读取 Westock 缓存…</div></div>;
  if (error) return <div className="card section-card"><div className="card-title compact">{title}</div><div className="alert alert-error">无法读取该区域；不影响本地研究主链。</div></div>;
  const allUnavailable = availability && Object.values(availability).every((s) => s === 'unavailable');
  return (
    <div className="card section-card">
      <div className="connection-group-header">
        <div><div className="card-title compact">{title}</div><div className="muted">Westock 缓存导出 · 非实时 · 仅研究展示</div></div>
        {availability ? <StatusRow availability={availability} /> : null}
      </div>
      {allUnavailable ? <p className="body-copy muted">该区域无 Westock 缓存；由 WorkBuddy 导出缓存后展示。</p> : children}
      {warnings?.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// 6 个深度 Tab 组件（挂载时才发起请求；staleTime 5 分钟）
// ---------------------------------------------------------------------- //

export function FundamentalsTab({ symbol }: { symbol: string }) {
  const q = useQuery({
    queryKey: ['stock-fundamentals', symbol],
    queryFn: () => api.stocksFundamentals(symbol),
    staleTime: STALE_TIME,
  });
  const d = q.data as StockFundamentalsResponse | undefined;
  return (
    <SectionWrapper title="基本面" loading={q.isLoading} error={q.isError}
      availability={d?.availability} warnings={d?.warnings}>
      <div className="deep-grid">
        <ProfileCard data={d?.data.profile} meta={d?.capability_meta.profile} />
        <FinancialSummary data={d?.data.financials} meta={d?.capability_meta.financials} />
        <ForecastCard data={d?.data.forecast} meta={d?.capability_meta.forecast} />
      </div>
    </SectionWrapper>
  );
}

export function OwnershipTab({ symbol }: { symbol: string }) {
  const q = useQuery({
    queryKey: ['stock-ownership', symbol],
    queryFn: () => api.stocksOwnership(symbol),
    staleTime: STALE_TIME,
  });
  const d = q.data as StockOwnershipResponse | undefined;
  return (
    <SectionWrapper title="股东回报" loading={q.isLoading} error={q.isError}
      availability={d?.availability} warnings={d?.warnings}>
      <div className="deep-grid">
        <ShareholderTable data={d?.data.shareholders} meta={d?.capability_meta.shareholders} />
        <DividendCard data={d?.data.dividend} meta={d?.capability_meta.dividend} />
        <BuybackCard data={d?.data.buyback} meta={d?.capability_meta.buyback} />
      </div>
    </SectionWrapper>
  );
}

export function FundsTab({ symbol }: { symbol: string }) {
  const q = useQuery({
    queryKey: ['stock-funds', symbol],
    queryFn: () => api.stocksFunds(symbol),
    staleTime: STALE_TIME,
  });
  const d = q.data as StockFundsResponse | undefined;
  return (
    <SectionWrapper title="资金" loading={q.isLoading} error={q.isError}
      availability={d?.availability} warnings={d?.warnings}>
      <div className="deep-grid">
        <FundFlowCard data={d?.data.fund_flow} meta={d?.capability_meta.fund_flow} />
        <MarginCard data={d?.data.margin} meta={d?.capability_meta.margin} />
        <NorthboundCard data={d?.data.northbound} meta={d?.capability_meta.northbound} />
        <ChipSummary data={d?.data.chip_distribution} meta={d?.capability_meta.chip_distribution} />
        <BlockTradeTable data={d?.data.block_trade} meta={d?.capability_meta.block_trade} />
        <LhbTable data={d?.data.lhb} meta={d?.capability_meta.lhb} />
      </div>
    </SectionWrapper>
  );
}

export function IntelTab({ symbol }: { symbol: string }) {
  const [category, setCategory] = useState('');
  const [offset, setOffset] = useState(0);
  const limit = 10;
  const q = useQuery({
    queryKey: ['stock-intel', symbol, category, offset],
    queryFn: () => api.stocksIntel(symbol, { category: category || undefined, limit, offset }),
    staleTime: STALE_TIME,
  });
  const d = q.data as StockIntelResponse | undefined;
  const total = typeof d?.data.total === 'number' ? d.data.total : 0;
  return (
    <SectionWrapper title="资讯公告" loading={q.isLoading} error={q.isError}
      availability={d?.availability} warnings={d?.warnings}>
      <div className="stock-chart-controls">
        <div className="btn-group">
          <button className={`btn btn-sm${category === '' ? ' btn-active' : ''}`} onClick={() => { setCategory(''); setOffset(0); }}>全部</button>
          {INTEL_FILTERS.map((f) => (
            <button key={f.key} className={`btn btn-sm${category === f.key ? ' btn-active' : ''}`} onClick={() => { setCategory(f.key); setOffset(0); }}>{f.label}</button>
          ))}
        </div>
        <div className="btn-group">
          <button className="btn btn-sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</button>
          <button className="btn btn-sm" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>下一页</button>
        </div>
      </div>
      <IntelList items={d?.data.items ?? []} category={category} metas={d?.capability_meta} />
      <span className="muted">共 {total} 条</span>
    </SectionWrapper>
  );
}

export function EventsTab({ symbol }: { symbol: string }) {
  const q = useQuery({
    queryKey: ['stock-events', symbol],
    queryFn: () => api.stocksEvents(symbol),
    staleTime: STALE_TIME,
  });
  const d = q.data as StockEventsResponse | undefined;
  return (
    <SectionWrapper title="风险事件" loading={q.isLoading} error={q.isError}
      availability={d?.availability} warnings={d?.warnings}>
      <div className="deep-grid">
        <EventsList data={d?.data.events} meta={d?.capability_meta.events} />
        <RiskList data={d?.data.risk} meta={d?.capability_meta.risk} />
      </div>
    </SectionWrapper>
  );
}

export function TechnicalTab({ symbol }: { symbol: string }) {
  const q = useQuery({
    queryKey: ['stock-technical', symbol],
    queryFn: () => api.stocksTechnical(symbol),
    staleTime: STALE_TIME,
  });
  const d = q.data as StockTechnicalResponse | undefined;
  return (
    <SectionWrapper title="技术指标" loading={q.isLoading} error={q.isError}
      availability={d?.availability} warnings={d?.warnings}>
      <TechnicalPanel data={d?.data.indicators} meta={d?.capability_meta.technical} note={d?.data.note as string | undefined} />
    </SectionWrapper>
  );
}
