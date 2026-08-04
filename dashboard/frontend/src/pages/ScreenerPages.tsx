import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  api,
  Candidate,
  ScreenerMode,
  ScreenerQuery,
  ScreenerRow,
  ScreenerRunResponse,
} from '../api/client';
import { fmtIsoTime, fmtNum, fmtText, CollapsibleText } from '../components/StrongCards';
import { PageHeader } from '../components/ui';

const STALE_TIME = 5 * 60 * 1000;
const SCREENER_DISCLAIMER = '选股结果仅用于研究：不生成 BigA 信号、不创建订单或持仓、不同步到 Westock 自选股。';

// 中文标签映射（用户可见；内部协议值保持英文）
const MODE_LABEL: Record<string, string> = {
  condition: '条件选股', strategy: '策略选股', factor: '因子排行', label: '标签选股',
};
const FIELD_LABEL: Record<string, string> = {
  price: '现价', change_percent: '涨跌幅', volume: '成交量', amount: '成交额',
  turnover_rate: '换手率', volume_ratio: '量比', market_cap: '总市值',
  float_market_cap: '流通市值', pe: '市盈率', pb: '市净率', ps: '市销率',
  dividend_yield: '股息率', roe: '净资产收益率', revenue_growth: '营收增速',
  profit_growth: '利润增速', debt_ratio: '负债率', operating_cash_flow: '经营现金流',
  ma5_above_ma20: 'MA5 高于 MA20', macd_signal: 'MACD 信号', rsi: 'RSI',
  kdj_k: 'KDJ-K', main_fund_flow: '主力资金流',
  northbound_change: '北向变化', margin_change: '两融变化',
};
const OP_LABEL: Record<string, string> = {
  gt: '大于', gte: '大于等于', lt: '小于', lte: '小于等于', eq: '等于', between: '介于', in: '属于',
};
const STRATEGY_LABEL: Record<string, string> = {
  ma_breakout: '均线突破', macd_golden_cross: 'MACD 金叉', rsi_oversold: 'RSI 超卖',
  boll_breakout: '布林突破', volume_breakout: '放量突破', trend_strength: '趋势强度',
  value_quality: '价值质量', growth_quality: '成长质量',
};
const STRATEGY_PARAM_LABEL: Record<string, string> = {
  lookback_days: '回看天数（1-250）', threshold: '阈值（-100~100）',
  min_volume_ratio: '最小量比（0-100）', rsi_limit: 'RSI 阈值（0-100）',
};
const FACTOR_LABEL: Record<string, string> = {
  value: '价值', quality: '质量', growth: '成长', momentum: '动量',
  volatility: '波动', liquidity: '流动性', size: '规模', dividend: '分红', composite: '综合',
};
const LABEL_LABEL: Record<string, string> = {
  high_dividend: '高股息', low_valuation: '低估值', institutional_focus: '机构关注',
  northbound_heavy: '北向重仓', active_fund_flow: '资金活跃', earnings_growth: '业绩增长',
  buyback: '回购', dividend_plan: '分红预案', risk_warning: '风险提示',
  hot_stock: '热门股', sector_leader: '板块龙头',
};
const UNIVERSE_LABEL: Record<string, string> = {
  local: '本地标的', index: '指数成分', sector: '板块', industry_chain: '产业链',
};

const NUMERIC_FIELDS = [
  'price', 'change_percent', 'volume', 'amount', 'turnover_rate', 'volume_ratio',
  'market_cap', 'float_market_cap', 'pe', 'pb', 'ps', 'dividend_yield', 'roe',
  'revenue_growth', 'profit_growth', 'debt_ratio', 'operating_cash_flow',
  'rsi', 'kdj_k', 'main_fund_flow', 'northbound_change', 'margin_change',
];
const BOOLEAN_FIELDS = ['ma5_above_ma20'];
const MACD_SIGNAL_ENUM = [
  { value: 'golden_cross', label: '金叉' }, { value: 'death_cross', label: '死叉' },
  { value: 'bullish', label: '看多' }, { value: 'bearish', label: '看空' }, { value: 'neutral', label: '中性' },
];
const ENUM_FIELDS: Record<string, { value: string; label: string }[]> = { macd_signal: MACD_SIGNAL_ENUM };
const STRATEGIES = ['ma_breakout', 'macd_golden_cross', 'rsi_oversold', 'boll_breakout',
  'volume_breakout', 'trend_strength', 'value_quality', 'growth_quality'];
const STRATEGY_ALLOWED_PARAMS: Record<string, string[]> = {
  ma_breakout: ['lookback_days'], macd_golden_cross: ['lookback_days'], rsi_oversold: ['rsi_limit'],
  boll_breakout: ['lookback_days'], volume_breakout: ['min_volume_ratio', 'lookback_days'],
  trend_strength: ['lookback_days', 'threshold'], value_quality: ['threshold'], growth_quality: ['threshold'],
};
const FACTORS = ['value', 'quality', 'growth', 'momentum', 'volatility', 'liquidity', 'size', 'dividend', 'composite'];
const LABELS = [
  'high_dividend', 'low_valuation', 'institutional_focus', 'northbound_heavy',
  'active_fund_flow', 'earnings_growth', 'buyback', 'dividend_plan',
  'risk_warning', 'hot_stock', 'sector_leader',
];

interface ConditionRow { field: string; operator: string; lo: string; hi: string; boolVal: boolean; enumSel: string[]; }
interface StrategyRow { name: string; params: Record<string, string>; }
interface FactorRow { name: string; topN: number; ascending: boolean; weights: Record<string, string>; }

function emptyCondition(): ConditionRow {
  return { field: 'price', operator: 'gt', lo: '0', hi: '100', boolVal: true, enumSel: ['golden_cross'] };
}
function emptyStrategyRow(): StrategyRow {
  return { name: 'ma_breakout', params: { lookback_days: '20' } };
}
function emptyFactorRow(): FactorRow {
  return { name: 'value', topN: 50, ascending: false, weights: { value: '0.5', growth: '0.5' } };
}

// ---------------------------------------------------------------------- //
// 执行 Tab（条件/策略/标签/因子 共用）
// ---------------------------------------------------------------------- //

function RunForm({ mode, initial, onResult }: {
  mode: ScreenerMode;
  initial?: ScreenerQuery | null;
  onResult: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [universeType, setUniverseType] = useState<'local' | 'index' | 'sector' | 'industry_chain'>(
    ((initial?.universe.type as 'local' | 'index' | 'sector' | 'industry_chain') ?? 'local'));
  const [universeValue, setUniverseValue] = useState(initial?.universe.value ?? '');
  const [limit, setLimit] = useState(initial?.limit ?? 50);
  const [conditions, setConditions] = useState<ConditionRow[]>(
    (initial?.conditions ?? []).map((c) => {
      if (ENUM_FIELDS[c.field]) {
        return { field: c.field, operator: c.operator === 'in' ? 'in' : 'eq',
                 lo: '', hi: '', boolVal: false,
                 enumSel: Array.isArray(c.value) ? c.value.map(String) : [String(c.value)] };
      }
      return { field: c.field, operator: c.operator,
               lo: Array.isArray(c.value) ? String(c.value[0]) : String(c.value ?? 0),
               hi: Array.isArray(c.value) ? String(c.value[1] ?? 0) : '100',
               boolVal: c.value === true, enumSel: [] };
    }));
  const [strategyRow, setStrategyRow] = useState<StrategyRow>(
    initial?.strategy ? { name: initial.strategy.name as string, params: initial.strategy as unknown as Record<string, string> } : emptyStrategyRow());
  const [factorRow, setFactorRow] = useState<FactorRow>(
    initial?.factor ? {
      name: initial.factor.name as string,
      topN: Number(initial.factor.top_n ?? 50),
      ascending: Boolean(initial.factor.ascending),
      weights: (initial.factor.weights as Record<string, number> | undefined) ? Object.fromEntries(
        Object.entries(initial.factor.weights as Record<string, number>).map(([k, v]) => [k, String(v)])) : { value: '0.5', growth: '0.5' },
    } : emptyFactorRow());
  const [labelSel, setLabelSel] = useState<string[]>(initial?.labels?.values ?? []);
  const [match, setMatch] = useState<string>(initial?.labels?.match ?? 'any');
  const [sortField, setSortField] = useState(initial?.sort.field ?? 'score');
  const [sortDir, setSortDir] = useState(initial?.sort.direction ?? 'desc');

  // universe 下拉选项（受控 API 数据）
  const sectorsQ = useQuery({ queryKey: ['market-sectors'], queryFn: () => api.marketSectors(), staleTime: STALE_TIME, enabled: universeType === 'sector' });
  const indexesQ = useQuery({ queryKey: ['market-indexes'], queryFn: () => api.marketIndexes(), staleTime: STALE_TIME, enabled: universeType === 'index' });
  const chainsQ = useQuery({ queryKey: ['market-industry-chain'], queryFn: () => api.marketIndustryChain(), staleTime: STALE_TIME, enabled: universeType === 'industry_chain' });

  const mutation = useMutation({
    mutationFn: (query: ScreenerQuery) => api.screenerRun(query),
    onSuccess: (d: ScreenerRunResponse) => onResult(d.result_id),
  });
  const saveMutation = useMutation({
    mutationFn: () => api.screenerSavedCreate({ name: '未命名条件 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }), query: buildQuery() }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['screener-saved'] }); },
  });

  function addCondition() {
    setConditions((prev) => [...prev, emptyCondition()].slice(-20));
  }

  function buildQuery(): ScreenerQuery {
    const q: ScreenerQuery = {
      mode,
      universe: { type: universeType, value: universeType === 'local' ? null : universeValue || null },
      conditions: [],
      strategy: null,
      factor: null,
      labels: null,
      sort: { field: sortField, direction: sortDir },
      limit: Number(limit),  // 校验已保证 1–200 整数，不静默 clamp
    };
    if (mode === 'condition') {
      q.conditions = conditions.map((c) => {
        if (BOOLEAN_FIELDS.includes(c.field)) {
          return { field: c.field, operator: 'eq', value: c.boolVal };
        }
        if (ENUM_FIELDS[c.field]) {
          if (c.operator === 'in') {
            return { field: c.field, operator: 'in', value: c.enumSel };
          }
          return { field: c.field, operator: 'eq', value: c.enumSel[0] ?? ENUM_FIELDS[c.field][0].value };
        }
        if (c.operator === 'between') {
          return { field: c.field, operator: 'between', value: [Number(c.lo), Number(c.hi)] };
        }
        return { field: c.field, operator: c.operator, value: Number(c.lo) };
      });
    }
    if (mode === 'strategy') {
      const params: Record<string, unknown> = {};
      for (const key of STRATEGY_ALLOWED_PARAMS[strategyRow.name] ?? []) {
        const v = Number(strategyRow.params[key]);
        if (Number.isFinite(v)) params[key] = v;
      }
      q.strategy = { name: strategyRow.name, ...params };
    }
    if (mode === 'factor') {
      const factor: Record<string, unknown> = { name: factorRow.name, top_n: factorRow.topN, ascending: factorRow.ascending };
      if (factorRow.name === 'composite') {
        const weights: Record<string, number> = {};
        for (const [k, v] of Object.entries(factorRow.weights)) {
          const n = Number(v);
          if (Number.isFinite(n)) weights[k] = n;
        }
        factor.weights = weights;
      }
      q.factor = factor;
    }
    if (mode === 'label') {
      q.labels = { values: labelSel, match };
    }
    return q;
  }

  // 统一前端校验：有错误时禁用执行与保存，显示中文原因；不静默篡改用户条件
  function buildValidation(): string | null {
    if (universeType !== 'local' && !universeValue) return '请先选择具体股票池（指数/板块/产业链）';
    const limitNum = Number(limit);
    if (!Number.isInteger(limitNum) || limitNum < 1 || limitNum > 200) return '数量必须为 1–200 整数';
    if (mode === 'condition') {
      for (const c of conditions) {
        if (ENUM_FIELDS[c.field]) {
          if (c.operator === 'in') {
            if (!c.enumSel.length) return '枚举多选至少选择一个值';
          } else if (!c.enumSel[0]) {
            return '枚举条件必须选择一个值';
          }
          continue;
        }
        if (BOOLEAN_FIELDS.includes(c.field)) continue;
        if (c.operator === 'between') {
          if (!c.lo.trim() || !c.hi.trim()) return '数值条件不能为空';
          const lo = Number(c.lo); const hi = Number(c.hi);
          if (!Number.isFinite(lo) || !Number.isFinite(hi)) return '数值条件必须为有效数字';
          if (lo > hi) return '介于范围前值必须小于等于后值';
        } else {
          if (!c.lo.trim()) return '数值条件不能为空';
          if (!Number.isFinite(Number(c.lo))) return '数值条件必须为有效数字';
        }
      }
    }
    if (mode === 'strategy') {
      for (const key of STRATEGY_ALLOWED_PARAMS[strategyRow.name] ?? []) {
        const raw = String(strategyRow.params[key] ?? '').trim();
        if (!raw) return '策略参数不能为空';
        const n = Number(raw);
        if (!Number.isFinite(n)) return '策略参数必须为有效数字';
        if (key === 'lookback_days' && (!Number.isInteger(n) || n < 1 || n > 250)) return '回看天数必须为 1–250 整数';
        if (key === 'threshold' && (n < -100 || n > 100)) return '阈值必须为 -100～100';
        if (key === 'min_volume_ratio' && (n < 0 || n > 100)) return '最小量比必须为 0～100';
        if (key === 'rsi_limit' && (n < 0 || n > 100)) return 'RSI 阈值必须为 0～100';
      }
    }
    if (mode === 'factor') {
      const topN = Number(factorRow.topN);
      if (!Number.isInteger(topN) || topN < 1 || topN > 200) return '因子数量必须为 1–200 整数';
    }
    if (mode === 'factor' && factorRow.name === 'composite') {
      let sum = 0;
      for (const v of Object.values(factorRow.weights)) {
        const raw = String(v).trim();
        if (!raw) continue;  // 空权重按 0 计（不报错）
        const n = Number(raw);
        if (!Number.isFinite(n)) return '综合因子权重必须为有效数字';
        if (n < 0) return '综合因子权重不能为负';
        sum += n;
      }
      if (sum <= 0) return '综合因子权重总和必须大于 0';
    }
    if (mode === 'label' && !labelSel.length) return '标签选股至少选择一个标签';
    return null;
  }
  const validationError = buildValidation();
  const errorMessage = mutation.isError ? (mutation.error as { message?: string })?.message ?? '执行失败' : null;
  const universeOptions = universeType === 'index'
    ? (indexesQ.data?.data.indexes ?? []).map((i) => ({ value: i.code ?? '', label: `${i.name ?? ''}（${i.code ?? ''}）` }))
    : universeType === 'sector'
      ? (sectorsQ.data?.data.sectors ?? []).map((s) => ({ value: s.code ?? '', label: `${s.name ?? ''}（${s.code ?? ''}）` }))
      : universeType === 'industry_chain'
        ? (chainsQ.data?.data.chains ?? []).map((c) => ({ value: c.code ?? '', label: `${c.name ?? ''}（${c.code ?? ''}）` }))
        : [];

  return (
    <div className="card section-card">
      <div className="card-title compact">{MODE_LABEL[mode]}</div>
      <div className="muted">{SCREENER_DISCLAIMER}</div>

      {/* 股票池：受控下拉，无任意自由文本 */}
      <div className="deep-fields-inline">
        <label className="muted">股票池：
          <select className="input" value={universeType} onChange={(e) => { setUniverseType(e.target.value as typeof universeType); setUniverseValue(''); }}>
            <option value="local">{UNIVERSE_LABEL.local}</option>
            <option value="index">{UNIVERSE_LABEL.index}</option>
            <option value="sector">{UNIVERSE_LABEL.sector}</option>
            <option value="industry_chain">{UNIVERSE_LABEL.industry_chain}</option>
          </select>
        </label>
        {universeType !== 'local' && (
          universeOptions.length ? (
            <label className="muted">选择：
              <select className="input" value={universeValue} onChange={(e) => setUniverseValue(e.target.value)}>
                <option value="">请选择</option>
                {universeOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
          ) : <span className="muted">（无可用选项：需先存在对应缓存）</span>
        )}
        <label className="muted">数量：
          <input className="input" type="number" min={1} max={200} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
        </label>
        <label className="muted">排序：
          <select className="input" value={sortField} onChange={(e) => setSortField(e.target.value)}>
            <option value="score">得分</option><option value="rank">排名</option>
            <option value="price">现价</option><option value="change_percent">涨跌幅</option><option value="symbol">代码</option>
          </select>
          <select className="input" value={sortDir} onChange={(e) => setSortDir(e.target.value)}>
            <option value="desc">降序</option><option value="asc">升序</option>
          </select>
        </label>
      </div>

      {mode === 'condition' && (
        <div>
          <div className="muted">筛选条件（{conditions.length}/20）</div>
          {conditions.map((c, i) => (
            <div className="deep-fields-inline" key={i}>
              <select className="input" aria-label="条件字段" value={c.field} onChange={(e) => { const f = e.target.value; setConditions((prev) => prev.map((x, xi) => xi === i ? { ...x, field: f, operator: BOOLEAN_FIELDS.includes(f) ? 'eq' : ENUM_FIELDS[f] ? 'eq' : 'gt' } : x)); }}>
                {[...NUMERIC_FIELDS, ...BOOLEAN_FIELDS, ...Object.keys(ENUM_FIELDS)].map((f) => <option key={f} value={f}>{FIELD_LABEL[f] ?? f}</option>)}
              </select>
              {BOOLEAN_FIELDS.includes(c.field) ? (
                <select className="input" value={String(c.boolVal)} onChange={(e) => setConditions((prev) => prev.map((x, xi) => xi === i ? { ...x, boolVal: e.target.value === 'true' } : x))}>
                  <option value="true">是</option><option value="false">否</option>
                </select>
              ) : ENUM_FIELDS[c.field] ? (
                <>
                  <select className="input" aria-label="条件操作符" value={c.operator} onChange={(e) => setConditions((prev) => prev.map((x, xi) => xi === i ? { ...x, operator: e.target.value } : x))}>
                    <option value="eq">等于</option><option value="in">属于（可多选）</option>
                  </select>
                  {c.operator === 'in' ? (
                    <span className="deep-fields-inline">
                      {ENUM_FIELDS[c.field].map((opt) => (
                        <label key={opt.value} className="muted">
                          <input type="checkbox" checked={c.enumSel.includes(opt.value)}
                            onChange={() => setConditions((prev) => prev.map((x, xi) => xi === i ? {
                              ...x, enumSel: x.enumSel.includes(opt.value)
                                ? x.enumSel.filter((v) => v !== opt.value) : [...x.enumSel, opt.value] } : x))} />
                          {opt.label}
                        </label>
                      ))}
                    </span>
                  ) : (
                    <select className="input" aria-label="条件枚举值" value={c.enumSel[0] ?? ''} onChange={(e) => setConditions((prev) => prev.map((x, xi) => xi === i ? { ...x, enumSel: [e.target.value] } : x))}>
                      {ENUM_FIELDS[c.field].map((opt) => <option key={opt.value} value={opt.value}>{opt.label}</option>)}
                    </select>
                  )}
                </>
              ) : (
                <>
                  <select className="input" aria-label="条件操作符" value={c.operator} onChange={(e) => setConditions((prev) => prev.map((x, xi) => xi === i ? { ...x, operator: e.target.value } : x))}>
                    {(['gt', 'gte', 'lt', 'lte', 'eq', 'between'] as const).map((o) => <option key={o} value={o}>{OP_LABEL[o]}</option>)}
                  </select>
                  <input className="input" aria-label="条件值低" value={c.lo} onChange={(e) => setConditions((prev) => prev.map((x, xi) => xi === i ? { ...x, lo: e.target.value } : x))} />
                  {c.operator === 'between' && (
                    <input className="input" aria-label="条件值高" value={c.hi} onChange={(e) => setConditions((prev) => prev.map((x, xi) => xi === i ? { ...x, hi: e.target.value } : x))} />
                  )}
                </>
              )}
              <button className="btn btn-sm" onClick={() => setConditions((prev) => prev.filter((_, xi) => xi !== i))}>删除</button>
            </div>
          ))}
          <button className="btn btn-sm" onClick={addCondition}>添加条件</button>
        </div>
      )}

      {mode === 'strategy' && (
        <div className="deep-fields-inline">
          <label className="muted">策略：
            <select className="input" value={strategyRow.name} onChange={(e) => setStrategyRow({ name: e.target.value, params: {} })}>
              {STRATEGIES.map((s) => <option key={s} value={s}>{STRATEGY_LABEL[s]}</option>)}
            </select>
          </label>
          {(STRATEGY_ALLOWED_PARAMS[strategyRow.name] ?? []).map((key) => (
            <label className="muted" key={key}>{STRATEGY_PARAM_LABEL[key] ?? key}：
              <input className="input" value={strategyRow.params[key] ?? ''} onChange={(e) => setStrategyRow((prev) => ({ ...prev, params: { ...prev.params, [key]: e.target.value } }))} />
            </label>
          ))}
        </div>
      )}

      {mode === 'factor' && (
        <div className="deep-fields-inline">
          <label className="muted">因子：
            <select className="input" value={factorRow.name} onChange={(e) => setFactorRow((prev) => ({ ...prev, name: e.target.value }))}>
              {FACTORS.map((f) => <option key={f} value={f}>{FACTOR_LABEL[f]}</option>)}
            </select>
          </label>
          <label className="muted">数量（1-200）：
            <input className="input" type="number" min={1} max={200} value={factorRow.topN} onChange={(e) => setFactorRow((prev) => ({ ...prev, topN: Number(e.target.value) }))} />
          </label>
          <label className="muted">升序：
            <select className="input" value={String(factorRow.ascending)} onChange={(e) => setFactorRow((prev) => ({ ...prev, ascending: e.target.value === 'true' }))}>
              <option value="false">否</option><option value="true">是</option>
            </select>
          </label>
          {factorRow.name === 'composite' && (
            <div className="deep-fields-inline">
              {Object.entries(factorRow.weights).map(([k, v]) => (
                <label className="muted" key={k}>{FACTOR_LABEL[k] ?? k}：
                  <input className="input" value={v} onChange={(e) => setFactorRow((prev) => ({ ...prev, weights: { ...prev.weights, [k]: e.target.value } }))} />
                </label>
              ))}
            </div>
          )}
        </div>
      )}

      {mode === 'label' && (
        <div className="deep-fields-inline">
          <label className="muted">匹配：
            <select className="input" value={match} onChange={(e) => setMatch(e.target.value)}>
              <option value="any">任一</option><option value="all">全部</option>
            </select>
          </label>
          {LABELS.map((l) => (
            <label key={l} className="muted">
              <input type="checkbox" checked={labelSel.includes(l)} onChange={() => setLabelSel((prev) => (prev.includes(l) ? prev.filter((x) => x !== l) : [...prev, l]).slice(0, 10))} />
              {LABEL_LABEL[l]}
            </label>
          ))}
        </div>
      )}

      <div className="deep-fields-inline">
        <button className="btn btn-primary"
          disabled={mutation.isPending || !!validationError || (universeType !== 'local' && universeOptions.length === 0)}
          onClick={() => mutation.mutate(buildQuery())}>
          {mutation.isPending ? '执行中…' : (universeType !== 'local' && universeOptions.length === 0) ? '无可选股票池' : '执行选股'}
        </button>
        <button className="btn btn-sm" disabled={saveMutation.isPending || !!validationError} onClick={() => saveMutation.mutate()}>
          保存当前条件
        </button>
      </div>
      {validationError && <div className="alert alert-warning">{validationError}</div>}
      {saveMutation.isSuccess && <div className="alert alert-success">当前条件已保存到「已保存条件」。</div>}
      {saveMutation.isError && <div className="alert alert-error">保存失败：{(saveMutation.error as { message?: string })?.message ?? '未知错误'}</div>}
      {errorMessage && <div className="alert alert-error">{errorMessage}</div>}
      {mutation.isSuccess && <div className="alert alert-success">选股完成，正在进入结果页…</div>}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// 已保存条件 Tab（保存当前编辑 query + 载入恢复）
// ---------------------------------------------------------------------- //

export function SavedTab({ onLoad }: { onLoad: (q: ScreenerQuery) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['screener-saved'], queryFn: () => api.screenerSavedList(), staleTime: STALE_TIME });
  const del = useMutation({
    mutationFn: (id: string) => api.screenerSavedDelete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['screener-saved'] }); },
  });
  return (
    <div className="card section-card">
      <div className="card-title compact">已保存条件</div>
      <div className="muted">在选股 Tab 点击「保存当前条件」即可保存当前编辑的查询；此处可载入/删除。</div>
      <div className="muted">保存条件不会自动执行选股。</div>
      {q.isLoading && <div className="loading-state">正在读取…</div>}
      {q.isError && <div className="alert alert-error">无法读取已保存条件。</div>}
      {!q.isLoading && !q.isError && (q.data?.items.length ? (
        <ul className="stock-list">
          {q.data.items.map((item) => (
            <li key={item.id}>
              <strong>{fmtText(item.name)}</strong> · {MODE_LABEL[item.query?.mode ?? ''] ?? '条件'} · {fmtIsoTime(item.created_at)}
              <button className="btn btn-sm" onClick={() => onLoad(item.query)}>载入</button>
              <button className="btn btn-sm" onClick={() => del.mutate(item.id)}>删除</button>
            </li>
          ))}
        </ul>
      ) : <p className="body-copy muted">暂无已保存条件。</p>)}
      {del.isSuccess && <div className="alert alert-success">已删除。</div>}
      {del.isError && <div className="alert alert-error">删除失败：{(del.error as { message?: string })?.message ?? '未知错误'}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// 研究候选 Tab
// ---------------------------------------------------------------------- //

export function CandidatesTab() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['screener-candidates'], queryFn: () => api.screenerCandidatesList(), staleTime: STALE_TIME });
  const del = useMutation({
    mutationFn: (symbol: string) => api.screenerCandidatesDelete(symbol),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['screener-candidates'] }); },
  });
  return (
    <div className="card section-card">
      <div className="card-title compact">研究候选</div>
      <div className="muted">{q.data?.note ?? '研究候选列表仅用于人工研究整理，不会生成 BigA 信号、订单或持仓，也不会同步到 Westock 自选股。'}</div>
      {q.isLoading && <div className="loading-state">正在读取…</div>}
      {q.isError && <div className="alert alert-error">无法读取研究候选。</div>}
      {!q.isLoading && !q.isError && (q.data?.items.length ? (
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>代码</th><th>名称</th><th>备注</th><th>本地 K 线</th><th>添加时间</th><th /></tr></thead>
            <tbody>
              {q.data.items.map((c: Candidate, i) => (
                <tr key={i}>
                  <td>{c.symbol && c.local_history_available
                    ? <Link to={`/stocks/${encodeURIComponent(c.symbol)}`}>{c.symbol}</Link>
                    : <span>{c.symbol}{c.symbol && !c.local_history_available ? '（尚未补跑）' : ''}</span>}</td>
                  <td>{fmtText(c.name)}</td>
                  <td>{c.note ? <CollapsibleText text={fmtText(c.note)} /> : '暂无'}</td>
                  <td>{c.local_history_available ? '本地数据可用' : '尚未补跑'}</td>
                  <td>{fmtIsoTime(c.added_at)}</td>
                  <td><button className="btn btn-sm" onClick={() => del.mutate(c.symbol)}>移除</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="body-copy muted">暂无研究候选。</p>)}
      {del.isSuccess && <div className="alert alert-success">已移除。</div>}
      {del.isError && <div className="alert alert-error">移除失败：{(del.error as { message?: string })?.message ?? '未知错误'}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------- //
// 结果页（中文查询摘要）
// ---------------------------------------------------------------------- //

export function ScreenerResultPage() {
  const { id = '' } = useParams();
  const q = useQuery({ queryKey: ['screener-result', id], queryFn: () => api.screenerResult(id), staleTime: STALE_TIME, retry: false });
  const qc = useQueryClient();
  const add = useMutation({
    mutationFn: (symbol: string) => api.screenerCandidatesAdd({ symbol, source_result_id: id, note: '来自选股结果' }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['screener-candidates'] }); },
  });
  const d = q.data as ScreenerRunResponse | undefined;
  const rows = d?.data.items ?? [];
  const query = d?.query;
  const summaryParts: string[] = [];
  if (query) {
    summaryParts.push(`模式：${MODE_LABEL[query.mode] ?? query.mode}`);
    summaryParts.push(`股票池：${UNIVERSE_LABEL[query.universe.type] ?? query.universe.type}${query.universe.value ? `（${query.universe.value}）` : ''}`);
    if (query.mode === 'condition' && query.conditions?.length) {
      summaryParts.push(`条件：${query.conditions.map((c) => `${FIELD_LABEL[c.field] ?? c.field} ${OP_LABEL[c.operator] ?? c.operator} ${Array.isArray(c.value) ? c.value.join('~') : String(c.value)}`).join('；')}`);
    }
    if (query.mode === 'strategy' && query.strategy) {
      summaryParts.push(`策略：${STRATEGY_LABEL[query.strategy.name as string] ?? query.strategy.name}`);
    }
    if (query.mode === 'factor' && query.factor) {
      summaryParts.push(`因子：${FACTOR_LABEL[query.factor.name as string] ?? query.factor.name}`);
    }
    if (query.mode === 'label' && query.labels) {
      summaryParts.push(`标签：${query.labels.values.map((l) => LABEL_LABEL[l] ?? l).join('、')}（${query.labels.match === 'all' ? '全部' : '任一'}）`);
    }
    summaryParts.push(`排序：${query.sort.field === 'score' ? '得分' : query.sort.field} ${query.sort.direction === 'desc' ? '降序' : '升序'}`);
  }
  return (
    <div>
      <PageHeader title="选股结果" description="仅用于研究，不生成信号/订单/持仓" />
      <div className="card section-card">
        {q.isLoading && <div className="loading-state">正在读取结果…</div>}
        {q.isError && <div className="alert alert-error">结果不存在或已被清理（result_not_found）。</div>}
        {!q.isLoading && !q.isError && d && (
          <>
            <div className="deep-fields-inline">
              {summaryParts.map((part, i) => <span className="deep-chip" key={i}>{part}</span>)}
            </div>
            <div className="deep-fields-inline">
              <span className="deep-chip">数据日期 {d.as_of ?? '—'}</span>
              <span className="deep-chip">抓取 {fmtIsoTime(d.source_fetched_at)}</span>
              <span className="deep-chip">生成 {fmtIsoTime(d.generated_at)}</span>
              <span className="deep-chip">缓存状态 {d.cache_status === 'fresh' ? '新鲜' : d.cache_status === 'stale' ? '已过期' : '无缓存'}</span>
              {d.cache_scope ? <span className="deep-chip">缓存范围 {d.cache_scope}</span> : null}
            </div>
            {d.cache_status === 'unavailable' && (
              <div className="alert alert-warning">
                未执行实时查询：缺少与该筛选条件精确匹配的 Westock 缓存。
                需要通过 WorkBuddy 为当前筛选条件导出缓存（缓存范围：{d.cache_scope ?? '—'}）。
              </div>
            )}
            {d.warnings.map((w) => <div className="alert alert-warning" key={w}>{w}</div>)}
            {rows.length ? (
              <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>排名</th><th>代码</th><th>名称</th><th>得分</th><th>现价</th><th>涨跌幅</th><th>行业</th><th>理由</th><th>研究候选</th></tr></thead>
                  <tbody>
                    {rows.map((r: ScreenerRow, i) => (
                      <tr key={i}>
                        <td>{r.rank ?? i + 1}</td>
                        <td>{r.symbol && r.local_history_available
                          ? <Link to={`/stocks/${encodeURIComponent(r.symbol)}`}>{r.symbol}</Link>
                          : <span>{r.symbol ?? '暂无'}{r.symbol && !r.local_history_available ? '（尚未补跑）' : ''}</span>}</td>
                        <td>{fmtText(r.name)}</td>
                        <td>{r.score != null ? fmtNum(r.score) : '暂无'}</td>
                        <td>{r.price != null ? `${fmtNum(r.price)} 元` : '暂无'}</td>
                        <td style={{ color: Number(r.change_percent ?? 0) >= 0 ? 'var(--color-danger)' : 'var(--color-success)' }}>
                          {r.change_percent != null ? `${fmtNum(r.change_percent)}%` : '暂无'}</td>
                        <td>{r.industry ?? '暂无'}</td>
                        <td>{r.reason ? <CollapsibleText text={fmtText(r.reason)} /> : '暂无'}</td>
                        <td><button className="btn btn-sm" disabled={!r.symbol || add.isPending} onClick={() => r.symbol && add.mutate(r.symbol)}>加入候选</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="body-copy muted">无选股结果（缓存无数据或未达标）。</p>}
            {add.isSuccess && <div className="alert alert-success">已加入研究候选。</div>}
            {add.isError && <div className="alert alert-error">加入候选失败：{(add.error as { message?: string })?.message ?? '未知错误'}</div>}
            <div className="muted">{SCREENER_DISCLAIMER}</div>
          </>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------- //
// /screener 页面：6 Tab
// ---------------------------------------------------------------------- //

export function ScreenerPage() {
  const [tab, setTab] = useState('condition');
  const [loadedQuery, setLoadedQuery] = useState<ScreenerQuery | null>(null);
  const [formKey, setFormKey] = useState(0);
  const TABS = [
    { key: 'condition', label: '条件选股', render: () => <RunForm key={`condition-${formKey}`} mode="condition" initial={loadedQuery} onResult={goResult} /> },
    { key: 'strategy', label: '策略选股', render: () => <RunForm key={`strategy-${formKey}`} mode="strategy" initial={loadedQuery} onResult={goResult} /> },
    { key: 'label', label: '标签选股', render: () => <RunForm key={`label-${formKey}`} mode="label" initial={loadedQuery} onResult={goResult} /> },
    { key: 'factor', label: '因子排行', render: () => <RunForm key={`factor-${formKey}`} mode="factor" initial={loadedQuery} onResult={goResult} /> },
    { key: 'saved', label: '已保存条件', render: () => <SavedTab onLoad={(q) => { setLoadedQuery(q); setTab(q.mode); setFormKey((k) => k + 1); }} /> },
    { key: 'candidates', label: '研究候选', render: () => <CandidatesTab /> },
  ];
  function goResult(resultId: string) {
    window.location.href = `/screener/results/${encodeURIComponent(resultId)}`;
  }
  return (
    <div>
      <PageHeader title="选股中心" description="Westock 缓存导出 · 仅研究 · 不生成信号/订单/持仓" />
      <div className="btn-group page-tabs">
        {TABS.map((t) => (
          <button key={t.key} className={`btn btn-sm${tab === t.key ? ' btn-active' : ''}`} onClick={() => setTab(t.key)}>{t.label}</button>
        ))}
      </div>
      {TABS.map((t) => (tab === t.key ? <div key={t.key}>{t.render()}</div> : null))}
    </div>
  );
}
