import * as echarts from 'echarts';
import { useEffect, useRef } from 'react';
import { useTheme } from '../theme/ThemeContext';

const UP_COLOR = '#e64545';
const DOWN_COLOR = '#2caa5b';
const BLUE = '#3b82f6';

function useMarketChart(option: echarts.EChartsOption | null) {
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
    if (!chart || !option) return;
    chart.setOption(option, { notMerge: true });
  }, [option, theme]);

  return { containerRef };
}

function baseOption(dark: boolean): Pick<echarts.EChartsOption, 'backgroundColor' | 'tooltip' | 'textStyle'> {
  const textColor = dark ? '#c8ccd4' : '#4a4a55';
  const splitColor = dark ? '#2a2e37' : '#e8e8ec';
  const bg = dark ? '#1a1d23' : '#ffffff';
  return {
    backgroundColor: bg,
    tooltip: {
      renderMode: 'richText' as const,
      backgroundColor: dark ? '#23262e' : '#ffffff',
      borderColor: splitColor,
      textStyle: { color: textColor, fontSize: 12 },
    },
    textStyle: { color: textColor },
  };
}

/** 涨跌分布柱状图（纯文本 tooltip，不拼 HTML）。 */
export function ChangeDistributionChart({ bins }: { bins: { label?: string; count?: number }[] }) {
  const { theme } = useTheme();
  const { containerRef } = useMarketChart(bins.length ? {
    ...baseOption(theme === 'dark'),
    animation: false,
    grid: { left: 48, right: 12, top: 12, bottom: 24 },
    xAxis: { type: 'category', data: bins.map((b) => b.label ?? ''), axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(128,128,128,0.15)' } } },
    series: [{ type: 'bar', data: bins.map((b) => b.count ?? 0), itemStyle: { color: BLUE }, barWidth: '60%' }],
  } : null);
  return <div ref={containerRef} className="market-chart" style={{ width: '100%', height: 260 }} />;
}

/** 市场维度评分：横向条形图。 */
export function MarketDimensionsChart({ dimensions }: { dimensions: Record<string, number | undefined> }) {
  const { theme } = useTheme();
  const labels: Record<string, string> = {
    trend: '趋势', sentiment: '情绪', liquidity: '流动性',
    breadth: '广度', volatility: '波动', risk: '风险',
  };
  const entries = Object.entries(dimensions).filter(([, v]) => typeof v === 'number') as [string, number][];
  const { containerRef } = useMarketChart(entries.length ? {
    ...baseOption(theme === 'dark'),
    animation: false,
    grid: { left: 64, right: 32, top: 8, bottom: 20 },
    xAxis: { type: 'value', min: 0, max: 100, splitLine: { lineStyle: { color: 'rgba(128,128,128,0.15)' } } },
    yAxis: { type: 'category', data: entries.map(([k]) => labels[k] ?? k) },
    series: [{ type: 'bar', data: entries.map(([, v]) => v), itemStyle: { color: BLUE }, barWidth: '55%' }],
  } : null);
  return <div ref={containerRef} className="market-chart" style={{ width: '100%', height: 240 }} />;
}

/** 板块表现：排序条形图（涨红跌绿）。 */
export function SectorBarChart({ sectors }: { sectors: { name?: string; change_percent?: number }[] }) {
  const { theme } = useTheme();
  const entries = sectors
    .filter((s) => typeof s.change_percent === 'number')
    .sort((a, b) => (b.change_percent ?? 0) - (a.change_percent ?? 0))
    .slice(0, 20);
  const { containerRef } = useMarketChart(entries.length ? {
    ...baseOption(theme === 'dark'),
    animation: false,
    grid: { left: 96, right: 40, top: 8, bottom: 20 },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(128,128,128,0.15)' } } },
    yAxis: { type: 'category', data: entries.map((s) => s.name ?? ''), axisLabel: { hideOverlap: true } },
    series: [{
      type: 'bar',
      data: entries.map((s) => ({
        value: s.change_percent ?? 0,
        itemStyle: { color: (s.change_percent ?? 0) >= 0 ? UP_COLOR : DOWN_COLOR },
      })),
      barWidth: '60%',
    }],
  } : null);
  return <div ref={containerRef} className="market-chart" style={{ width: '100%', height: Math.max(200, entries.length * 18) }} />;
}

/** 市场资金摘要条形图（仅合法数值时绘制）。 */
export function MarketFundsBar({ funds }: { funds: { margin_balance?: number | null; margin_change?: number | null; northbound_net?: number | null } }) {
  const { theme } = useTheme();
  const items = [
    { name: '融资余额', value: funds.margin_balance },
    { name: '北向净流入', value: funds.northbound_net },
  ].filter((i) => typeof i.value === 'number') as { name: string; value: number }[];
  const { containerRef } = useMarketChart(items.length ? {
    ...baseOption(theme === 'dark'),
    animation: false,
    grid: { left: 88, right: 40, top: 8, bottom: 20 },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(128,128,128,0.15)' } } },
    yAxis: { type: 'category', data: items.map((i) => i.name) },
    series: [{ type: 'bar', data: items.map((i) => i.value), itemStyle: { color: BLUE }, barWidth: '50%' }],
  } : null);
  return <div ref={containerRef} className="market-chart" style={{ width: '100%', height: 160 }} />;
}
