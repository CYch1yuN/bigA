import * as echarts from 'echarts';
import { useEffect, useRef } from 'react';
import { useTheme } from '../theme/ThemeContext';
import type { StockBar, StockOrderItem } from '../api/client';

export interface TradeMarker {
  date: string;
  side: 'BUY' | 'SELL';
  price: number;
  label: string;
}

interface Props {
  bars: StockBar[];
  adjustment: 'raw' | 'qfq';
  markers: TradeMarker[];
}

const UP_COLOR = '#e64545';   // A 股惯例：涨红
const DOWN_COLOR = '#2caa5b'; // 跌绿
const LIGHT_BG = '#ffffff';
const DARK_BG = '#1a1d23';

/** 把研究产物（订单）映射为 K 线上的真实买卖点；找不到对应日期则丢弃。 */
export function mapTradeMarkers(bars: StockBar[], orders: StockOrderItem[]): TradeMarker[] {
  const byDate = new Map<string, StockBar>();
  for (const bar of bars) byDate.set(bar.date, bar);
  const markers: TradeMarker[] = [];
  for (const order of orders) {
    const date = order.fill_date ?? order.signal_date;
    if (!date || order.side !== 'BUY' && order.side !== 'SELL') continue;
    const bar = byDate.get(date);
    if (!bar) continue; // 找不到对应日期不得猜测位置
    const price = order.fill_price ? Number(order.fill_price) : Number(bar.close);
    markers.push({ date, side: order.side, price, label: `${order.side} ${order.quantity ?? ''}` });
  }
  return markers;
}

export function EChartsKLine({ bars, adjustment, markers }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const { theme } = useTheme();

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current);
    chartRef.current = chart;
    const onResize = () => chart.resize();
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const dark = theme === 'dark';
    const textColor = dark ? '#c8ccd4' : '#4a4a55';
    const splitColor = dark ? '#2a2e37' : '#e8e8ec';
    const bg = dark ? DARK_BG : LIGHT_BG;

    const dates = bars.map((bar) => bar.date);
    const candleData = bars.map((bar) => [Number(bar.open), Number(bar.close), Number(bar.low), Number(bar.high)]);
    const volumeData = bars.map((bar) => ({
      value: bar.volume ?? 0,
      itemStyle: { color: Number(bar.close) >= Number(bar.open) ? UP_COLOR : DOWN_COLOR },
    }));
    // 买卖点：按日期映射到 K 线 index；找不到 index 的不画
    const markerIndex = new Map<string, number>();
    dates.forEach((d, i) => markerIndex.set(d, i));
    const markPointData = markers
      .filter((m) => markerIndex.has(m.date))
      .map((m) => {
        const idx = markerIndex.get(m.date)!;
        return {
          name: m.label,
          coord: [idx, m.price],
          value: m.label,
          itemStyle: { color: m.side === 'BUY' ? UP_COLOR : DOWN_COLOR },
          symbol: m.side === 'BUY' ? 'triangle' : 'pin',
          symbolSize: 14,
        };
      });

    chart.setOption(
      {
        backgroundColor: bg,
        animation: false,
        legend: { show: false },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross' },
          renderMode: 'richText', // 不渲染 HTML，纯文本
          backgroundColor: dark ? '#23262e' : '#ffffff',
          borderColor: splitColor,
          textStyle: { color: textColor, fontSize: 12 },
          formatter: (params: unknown) => {
            // 纯文本行数组：不拼接任何 HTML / marker 片段
            const list = Array.isArray(params) ? params : [params];
            const lines = list.map((p: { seriesType?: string; data?: unknown }) => {
              if (p.seriesType === 'candlestick' && Array.isArray(p.data)) {
                const [open, close, low, high] = p.data as number[];
                return `开 ${open.toFixed(2)} | 收 ${close.toFixed(2)} | 低 ${low.toFixed(2)} | 高 ${high.toFixed(2)}`;
              }
              if (p.seriesType === 'bar') {
                return `成交量 ${String(p.data)}`;
              }
              return String(p.data ?? '');
            });
            const title = (list[0] as { axisValueLabel?: string }).axisValueLabel ?? '';
            return [title, ...lines];
          },
        },
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        grid: [
          { left: 56, right: 16, top: 16, height: '62%' },
          { left: 56, right: 16, top: '76%', height: '16%' },
        ],
        xAxis: [
          {
            type: 'category', data: dates, gridIndex: 0,
            axisLine: { lineStyle: { color: splitColor } },
            axisLabel: { color: textColor, hideOverlap: true },
            axisTick: { show: false },
          },
          {
            type: 'category', data: dates, gridIndex: 1,
            axisLine: { lineStyle: { color: splitColor } },
            axisLabel: { show: false },
            axisTick: { show: false },
          },
        ],
        yAxis: [
          { scale: true, gridIndex: 0, axisLabel: { color: textColor }, splitLine: { lineStyle: { color: splitColor } } },
          { scale: true, gridIndex: 1, axisLabel: { color: textColor, formatter: (v: number) => (v >= 1e8 ? `${(v / 1e8).toFixed(1)}亿` : v >= 1e4 ? `${(v / 1e4).toFixed(0)}万` : String(v)) }, splitLine: { show: false } },
        ],
        dataZoom: [
          { type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - (60 / Math.max(bars.length, 1)) * 100), end: 100 },
          { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 14, borderColor: splitColor, textStyle: { color: textColor } },
        ],
        series: [
          {
            name: 'K线', type: 'candlestick', data: candleData,
            itemStyle: { color: UP_COLOR, color0: DOWN_COLOR, borderColor: UP_COLOR, borderColor0: DOWN_COLOR },
            markPoint: { data: markPointData, label: { color: '#ffffff', fontSize: 10 } },
          },
          { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumeData },
        ],
      },
      { notMerge: true },
    );
  }, [bars, adjustment, markers, theme]);

  return <div ref={containerRef} className="stock-chart" style={{ width: '100%', height: 420 }} />;
}
