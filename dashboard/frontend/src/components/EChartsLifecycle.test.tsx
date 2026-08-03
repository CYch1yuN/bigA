import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
import { ThemeProvider, useTheme } from '../theme/ThemeContext';
import type { StockBar, StockOrderItem } from '../api/client';
import { EChartsKLine, mapTradeMarkers, type TradeMarker } from './EChartsKLine';
import { EChartsMinuteChart } from './EChartsMinuteChart';

// mock echarts 模块：验证 init / setOption / resize / dispose 真实被调用
const h = vi.hoisted(() => {
  const setOption = vi.fn();
  const resize = vi.fn();
  const dispose = vi.fn();
  const init = vi.fn(() => ({ setOption, resize, dispose }));
  return { init, setOption, resize, dispose };
});
vi.mock('echarts', () => ({ init: h.init }));

const BARS: StockBar[] = [
  { date: '2026-07-30', open: '1323.00', high: '1362.00', low: '1322.00', close: '1361.76', volume: 7187261, amount: 9.7e9 },
  { date: '2026-07-31', open: '1330.03', high: '1355.72', low: '1325.77', close: '1350.60', volume: 5512752, amount: 7.3e9 },
];

function ThemeSwitchButton() {
  const { setTheme } = useTheme();
  return <button onClick={() => setTheme('light')}>switch-theme</button>;
}

function renderKLine(markers: TradeMarker[] = []) {
  return render(
    <ThemeProvider>
      <EChartsKLine bars={BARS} adjustment="qfq" markers={markers} />
    </ThemeProvider>,
  );
}

describe('EChartsKLine 生命周期（echarts 模块级 mock）', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('init 被调用并挂载容器', () => {
    const { container } = renderKLine();
    expect(h.init).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.stock-chart')).toBeTruthy();
  });

  it('setOption 包含 candlestick + volume + dataZoom + markPoint', async () => {
    const orders: StockOrderItem[] = [
      { signal_date: '2026-07-31', fill_date: '2026-07-31', symbol: '600519.SH', side: 'BUY', quantity: 100, status: 'FILLED', fill_price: '1350.00', reason: '' },
    ];
    const markers = mapTradeMarkers(BARS, orders);
    renderKLine(markers);
    // init → effect → setOption 至少一次
    expect(h.setOption).toHaveBeenCalled();
    const option = h.setOption.mock.calls[0][0];
    const series = option.series as unknown[];
    expect(series.some((s) => (s as { type?: string }).type === 'candlestick')).toBe(true);
    expect(series.some((s) => (s as { type?: string }).type === 'bar')).toBe(true);
    expect(option.dataZoom?.length).toBeGreaterThan(0);
    const candle = series.find((s) => (s as { type?: string }).type === "candlestick") as { markPoint?: { data: unknown[] } };
    expect(candle.markPoint?.data?.length).toBe(1);
  });

  it('markPoint 仅映射真实 K 线日期', () => {
    const orders: StockOrderItem[] = [
      { signal_date: '2025-01-01', fill_date: '2025-01-02', symbol: '600519.SH', side: 'BUY', quantity: 10, status: 'PENDING', fill_price: null, reason: '' },
    ];
    const markers = mapTradeMarkers(BARS, orders);
    expect(markers).toHaveLength(0);
  });

  it('主题更新触发重新 setOption', () => {
    const { container } = renderKLine();
    const callsAfterMount = h.setOption.mock.calls.length;
    // 包一层 ThemeSwitch：切换主题后 setOption 应再次被调用（新配色）
    const { container: c2 } = render(
      <ThemeProvider>
        <EChartsKLine bars={BARS} adjustment="raw" markers={[]} />
        <ThemeSwitchButton />
      </ThemeProvider>,
    );
    void container;
    const callsBeforeSwitch = h.setOption.mock.calls.length;
    expect(callsBeforeSwitch).toBeGreaterThanOrEqual(callsAfterMount + 1);
    fireEvent.click(c2.querySelector('button')!);
    expect(h.setOption.mock.calls.length).toBeGreaterThan(callsBeforeSwitch);
  });

  it('window resize 触发 chart.resize', () => {
    renderKLine();
    h.resize.mockClear();
    act(() => { window.dispatchEvent(new Event('resize')); });
    expect(h.resize).toHaveBeenCalledTimes(1);
  });

  it('卸载时 dispose 被调用', () => {
    const { unmount } = renderKLine();
    h.dispose.mockClear();
    unmount();
    expect(h.dispose).toHaveBeenCalledTimes(1);
  });
});

describe('EChartsMinuteChart 生命周期', () => {
  beforeEach(() => vi.clearAllMocks());

  it('init + 价格折线 + 成交量副图（含 volume 时）', () => {
    render(
      <ThemeProvider>
        <EChartsMinuteChart rows={[
          { time: '09:30', price: 100.0, volume: 100 },
          { time: '09:31', price: 100.5, volume: 150 },
        ]} />
      </ThemeProvider>,
    );
    expect(h.init).toHaveBeenCalledTimes(1);
    const option = h.setOption.mock.calls[0][0];
    const series = option.series as unknown[];
    expect(series.some((s) => (s as { type?: string }).type === 'line')).toBe(true);
    expect(series.some((s) => (s as { type?: string }).type === 'bar')).toBe(true); // 成交量副图
  });

  it('无 volume 时仅价格折线（无 bar 副图）', () => {
    render(
      <ThemeProvider>
        <EChartsMinuteChart rows={[{ time: '09:30', price: 100.0, volume: null }]} />
      </ThemeProvider>,
    );
    const option = h.setOption.mock.calls[0][0];
    const series = option.series as unknown[];
    expect(series.some((s) => (s as { type?: string }).type === 'line')).toBe(true);
    expect(series.some((s) => (s as { type?: string }).type === 'bar')).toBe(false);
  });

  it('卸载时 dispose', () => {
    const { unmount } = render(
      <ThemeProvider>
        <EChartsMinuteChart rows={[{ time: '09:30', price: 100.0, volume: null }]} />
      </ThemeProvider>,
    );
    h.dispose.mockClear();
    unmount();
    expect(h.dispose).toHaveBeenCalledTimes(1);
  });
});
