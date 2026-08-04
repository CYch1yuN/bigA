import { describe, expect, it, afterEach } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '../test/render';
import {
  BlockTradeTable, ChipSummary, EventsList, IntelList, LhbTable,
  MarginCard, NorthboundCard, RiskList,
} from './StrongCards';

// ---------------------------------------------------------------------- //
// 真实后端响应形状（F2-C 校准后 schema）
// ---------------------------------------------------------------------- //

const MARGIN = {
  date: '2026-08-04',
  financing_balance: 8.5e10,
  financing_buy: 1.2e9,
  financing_repay: 9.5e8,
  securities_lending_balance: 3.1e8,
  margin_balance: 8.53e10,
};

const NORTHBOUND = {
  unit_note: '单位说明：持股数量为股，持股市值为元，比例为 %。',
  current: {
    date: '2026-06-30', holding_shares: 8.5e7, holding_ratio: 6.8,
    holding_cap: 1.2e11, shares_change_q: -2e6, shares_change_y: 3e6,
    cap_change_q: -5e8, cap_change_y: 8e8,
  },
  previous: {
    date: '2026-03-31', holding_shares: 8.7e7, holding_ratio: 6.9,
    holding_cap: 1.25e11, shares_change_q: 1e6, shares_change_y: 2e6,
    cap_change_q: 3e8, cap_change_y: 6e8,
  },
};

const NORTHBOUND_ONE_SIDE = { current: NORTHBOUND.current, unit_note: NORTHBOUND.unit_note };

const BLOCK_TRADE = [
  { date: '2026-08-04', price: 10.49, amount: 93016900, discount_rate: 0, buyer: '机构专用', seller: '平安证券' },
];

const LHB = [
  { category: 'jg', date: '2026-07-29', td_days: 1, inst_buy_branches: 3, inst_buy_amount: 3.2e8,
    inst_buy_rate: 10.0, total_buy_amount: 5e8, net_buy_amount: 3.2e8, net_buy_rate: 6.0, rank: 1 },
  { category: 'yyb', date: '2026-07-29', name: '财通证券营业部', buy_amount: 1.1e8,
    symbols: ['600519.SH', '000001.SZ'] },
  { category: 'yzb', date: '2026-07-29', name: '章盟主', net_amount: 2.1e8,
    buy_stocks: [{ symbol: '600519.SH', name: '贵州茅台' }],
    sell_stocks: [{ symbol: '600519.SH', name: '贵州茅台' }] },
  { category: 'gslmr', date: '2026-07-29', td_days: 2, net_amount: 1.2e8, up_rate: 85.0,
    buy_amount: 2e8, sell_amount: 8e7, exchange_rate: 10.0, win_count: 5,
    branches: ['上海分公司', '北京分公司'] },
  { category: 'gslxw', date: '2026-07-29', name: '国泰君安', net_amount: 5e7, win_rate: 90.0,
    symbols: ['600519.SH'] },
];

const CHIP = {
  date: '2026-08-04', profit_ratio: 62.5, average_cost: 1450.2,
  concentration_90: 10.88, concentration_70: 6.2,
};

const RISK = {
  bond_ratings: [],
  executive_transfers: [],
  lawsuits: [
    { title: '白酒需求波动', level: '中', date: '2026-07-31',
      summary: '行业景气度下行', url: 'https://example.com/risk/1' },
  ],
  leader_changes: [{ title: '董事长变更', date: '2026-06-01' }],
  seasoned_issues: [],
  unlocks: [],
  pledge: { count: 2, ratio: 1.5, amount: 3e9 },
};

const EVENTS = [
  { category: 'events', date: '2026-08-20', title: '过去1个月内的大宗交易' },
];

const INTEL_ITEMS = [
  { category: 'reports', title: '维持买入评级', time: '2026-07-24 00:00:00', date: '2026-07-24',
    institution: '浙商证券', rating: '买入', target_price: 2200.0 },
  { category: 'announcements', title: '2026年中期分红公告', time: '2026-07-30 09:30:00',
    update_time: '2026-07-30 10:00:00', type: '分红', url: 'https://example.com/ann/1' },
  { category: 'news', title: '茅台发布半年报', source: '上证报', date: '2026-08-01',
    summary: '营收增长15%' },
];

const META = { status: 'stale', as_of: '2026-08-04', fetched_at: '2026-08-05T00:00:00+00:00', cache_age_seconds: 3600 } as never;

function expectNoRawGarbage() {
  const text = document.body.textContent ?? '';
  expect(text).not.toContain('undefined');
  expect(text).not.toContain('NaN');
  expect(text).not.toContain('[object Object]');
  expect(text).not.toContain('{');
}

function expectNoTradeButtons() {
  const buttons = screen.queryAllByRole('button');
  const bad = buttons.filter((b) => /交易|信号|订单/.test(b.textContent ?? ''));
  expect(bad).toHaveLength(0);
}

afterEach(() => {
  window.innerWidth = 1024;
});

describe('F2-C 前端卡片真实字段', () => {
  it('MarginCard 展示新 schema 字段，不读旧字段', () => {
    renderWithProviders(<MarginCard data={MARGIN} meta={META} />);
    expect(screen.getByText('融资买入')).toBeTruthy();
    expect(screen.getByText('融资偿还')).toBeTruthy();
    expect(screen.getByText('两融余额（融资+融券）')).toBeTruthy();
    expect(screen.getByText(/853\.00 亿元/)).toBeTruthy();
    expect(screen.queryByText('融资变化')).toBeNull();
    expect(screen.queryByText('融券变化')).toBeNull();
    expectNoRawGarbage();
  });

  it('NorthboundCard 展示 current/previous 与 unit_note', () => {
    renderWithProviders(<NorthboundCard data={NORTHBOUND} meta={META} />);
    expect(screen.getByText('本期')).toBeTruthy();
    expect(screen.getByText('上期')).toBeTruthy();
    expect(screen.getByText(/单位说明/)).toBeTruthy();
    expect(screen.getAllByText('较上季变化').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('市值较上年变化').length).toBeGreaterThanOrEqual(2);
    expectNoRawGarbage();
  });

  it('NorthboundCard 单侧缺失正常降级', () => {
    renderWithProviders(<NorthboundCard data={NORTHBOUND_ONE_SIDE} meta={META} />);
    expect(screen.getByText('本期')).toBeTruthy();
    expect(screen.getByText('上期：暂无。')).toBeTruthy();
    expectNoRawGarbage();
  });

  it('BlockTradeTable 展示 date/price/amount/discount_rate/buyer/seller，无股数列', () => {
    renderWithProviders(<BlockTradeTable data={BLOCK_TRADE} meta={META} />);
    expect(screen.getByText('折价率')).toBeTruthy();
    expect(screen.getByText('买方')).toBeTruthy();
    expect(screen.getByText('卖方')).toBeTruthy();
    expect(screen.getByText('机构专用')).toBeTruthy();
    expect(screen.queryByText('股数')).toBeNull();
    expectNoRawGarbage();
  });

  it('LhbTable 按分类中文展示 + 受控嵌套组件，无旧字段', () => {
    renderWithProviders(<LhbTable data={LHB} meta={META} />);
    expect(screen.getByText('机构专用')).toBeTruthy();
    expect(screen.getByText('营业部')).toBeTruthy();
    expect(screen.getByText('游资榜')).toBeTruthy();
    expect(screen.getByText('高胜率买入')).toBeTruthy();
    expect(screen.getByText('高胜率席位')).toBeTruthy();
    expect(screen.getByText('财通证券营业部')).toBeTruthy();
    expect(screen.getAllByText('600519.SH').length).toBeGreaterThanOrEqual(2); // yyb symbols + yzb buy_stocks
    expect(screen.getByText(/买入：600519\.SH 贵州茅台/)).toBeTruthy(); // yzb buy_stocks
    expect(screen.getByText(/营业部：上海分公司、北京分公司/)).toBeTruthy();
    expect(screen.queryByText('上榜原因')).toBeNull();
    expect(screen.queryByText('席位')).toBeNull();
    expectNoRawGarbage();
  });

  it('ChipSummary 展示标量对象 schema', () => {
    renderWithProviders(<ChipSummary data={CHIP} meta={META} />);
    expect(screen.getByText('获利比例')).toBeTruthy();
    expect(screen.getByText('平均成本')).toBeTruthy();
    expect(screen.getByText('90% 集中度')).toBeTruthy();
    expect(screen.getByText('70% 集中度')).toBeTruthy();
    expect(screen.queryByText('筹码集中度')).toBeNull();
    expect(screen.queryByText('价格分布')).toBeNull();
    expectNoRawGarbage();
  });

  it('EventsList 中文化：标题事件、不出现英文 category', () => {
    renderWithProviders(<EventsList data={EVENTS} meta={META} />);
    expect(screen.getByText('事件')).toBeTruthy(); // 卡片标题中文
    expect(screen.getByText('2026-08-20')).toBeTruthy();
    expect(screen.getByText('过去1个月内的大宗交易')).toBeTruthy();
    expect(screen.queryByText('events')).toBeNull(); // 不渲染英文 category
    expectNoRawGarbage();
  });

  it('IntelList reports 用 institution/rating/time；announcements 用 type/update_time/url；news 不受影响', () => {
    renderWithProviders(<IntelList items={INTEL_ITEMS} category="" metas={{ reports: META, announcements: META, news: META }} />);
    expect(screen.getByText(/浙商证券/)).toBeTruthy();
    expect(screen.getByText(/2026-07-24 00:00:00/)).toBeTruthy();
    expect(screen.getAllByText(/分红/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/更新 2026-07-30 10:00:00/)).toBeTruthy();
    expect(screen.getByText('查看原文')).toBeTruthy();
    expect(screen.getByText(/茅台发布半年报/)).toBeTruthy();
    expect(screen.getByText(/上证报/)).toBeTruthy();
    expectNoRawGarbage();
  });
});

describe('F2-C RiskList 对象响应', () => {
  it('对象响应不崩溃，显式展示 6 分类与 pledge', () => {
    renderWithProviders(<RiskList data={RISK} meta={META} />);
    expect(screen.getByText('诉讼')).toBeTruthy();
    expect(screen.getByText('白酒需求波动')).toBeTruthy();
    expect(screen.getByText('管理层变更')).toBeTruthy();
    expect(screen.getByText('董事长变更')).toBeTruthy();
    expect(screen.getByText('债券评级')).toBeTruthy();
    expect(screen.getByText('股权质押')).toBeTruthy();
    expect(screen.getByText(/质押比例/)).toBeTruthy();
    expect(screen.getByText(/1\.50%/)).toBeTruthy();
    expect(screen.getAllByText(/不替代人工核实/).length).toBeGreaterThanOrEqual(2);
    expectNoRawGarbage();
  });

  it('空对象响应 → 暂无，不崩溃', () => {
    renderWithProviders(<RiskList data={{}} meta={META} />);
    expect(screen.getByText('暂无风险提示。')).toBeTruthy();
    expectNoRawGarbage();
  });

  it('null 响应 → 暂无，不崩溃', () => {
    renderWithProviders(<RiskList data={null} meta={META} />);
    expect(screen.getByText('暂无风险提示。')).toBeTruthy();
    expectNoRawGarbage();
  });

  it('未知键不展示（不遍历）', () => {
    renderWithProviders(<RiskList data={{ ...RISK, mystery: { hacked: 1 }, secret: 'C:\\secret' }} meta={META} />);
    expect(screen.queryByText(/secret|hacked|C:\\secret/)).toBeNull();
    expectNoRawGarbage();
  });
});

describe('F2-C 全局约束', () => {
  it('卡片区域不存在交易/信号/订单按钮', () => {
    renderWithProviders(
      <div>
        <MarginCard data={MARGIN} meta={META} />
        <NorthboundCard data={NORTHBOUND} meta={META} />
        <BlockTradeTable data={BLOCK_TRADE} meta={META} />
        <LhbTable data={LHB} meta={META} />
        <ChipSummary data={CHIP} meta={META} />
        <RiskList data={RISK} meta={META} />
        <EventsList data={EVENTS} meta={META} />
        <IntelList items={INTEL_ITEMS} category="" metas={{ reports: META, announcements: META, news: META }} />
      </div>,
    );
    expectNoTradeButtons();
    expectNoRawGarbage();
  });

  it('深浅主题 + 移动端视口渲染不崩溃、无横向溢出', () => {
    // 深色主题（默认）+ 移动端
    window.innerWidth = 375;
    const { unmount } = renderWithProviders(
      <div>
        <MarginCard data={MARGIN} meta={META} />
        <NorthboundCard data={NORTHBOUND} meta={META} />
        <BlockTradeTable data={BLOCK_TRADE} meta={META} />
        <LhbTable data={LHB} meta={META} />
        <ChipSummary data={CHIP} meta={META} />
        <RiskList data={RISK} meta={META} />
        <EventsList data={EVENTS} meta={META} />
        <IntelList items={INTEL_ITEMS} category="" metas={{ reports: META, announcements: META, news: META }} />
      </div>,
    );
    expect(screen.getByText('融资买入')).toBeTruthy();
    expect(screen.getByText('白酒需求波动')).toBeTruthy();
    expectNoRawGarbage();
    expectNoTradeButtons();
    unmount();

    // 浅色主题：localStorage 置 light 后重渲染
    localStorage.setItem('ashare-dashboard-theme', 'light');
    renderWithProviders(
      <div>
        <RiskList data={RISK} meta={META} />
        <LhbTable data={LHB} meta={META} />
      </div>,
    );
    expect(screen.getByText('诉讼')).toBeTruthy();
    expect(screen.getByText('机构专用')).toBeTruthy();
    expectNoRawGarbage();
    localStorage.removeItem('ashare-dashboard-theme');
  });
});
