import * as echarts from 'echarts';
import { useEffect, useRef } from 'react';
import { useTheme } from '../theme/ThemeContext';

export interface MinuteRow {
  time: string;
  price: number;
  volume: number | null;
}

interface Props {
  rows: MinuteRow[];
}

const UP_COLOR = '#e64545';
const DOWN_COLOR = '#2caa5b';
const LINE_COLOR = '#3b82f6';

/** 分时图：受控标准化数据（time/price/volume）折线 + 可选成交量副图。
 *  缓存导出、非实时；仅缓存存在且有合法数据时展示。 */
export function EChartsMinuteChart({ rows }: Props) {
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
    const bg = dark ? '#1a1d23' : '#ffffff';
    const times = rows.map((r) => r.time);
    const prices = rows.map((r) => r.price);
    const hasVolume = rows.some((r) => r.volume != null);
    const ref = rows.length ? rows[0].price : 0;
    const volumeData = rows.map((r) => ({
      value: r.volume ?? 0,
      itemStyle: { color: r.price >= ref ? UP_COLOR : DOWN_COLOR },
    }));

    chart.setOption(
      {
        backgroundColor: bg,
        animation: false,
        title: { show: false },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: dark ? '#23262e' : '#ffffff',
          borderColor: splitColor,
          textStyle: { color: textColor, fontSize: 12 },
          formatter: (params: unknown) => {
            const list = Array.isArray(params) ? params : [params];
            const lines = list.map((p: { seriesType?: string; data?: unknown }) => {
              if (p.seriesType === 'bar') return `成交量 ${String(p.data)}`;
              return `价格 ${Number(p.data).toFixed(2)}`;
            });
            const title = (list[0] as { axisValueLabel?: string }).axisValueLabel ?? '';
            return [title, ...lines];
          },
        },
        grid: hasVolume
          ? [
              { left: 56, right: 16, top: 16, height: '62%' },
              { left: 56, right: 16, top: '76%', height: '16%' },
            ]
          : [{ left: 56, right: 16, top: 16, bottom: 24 }],
        xAxis: (hasVolume ? [0, 1] : [0]).map((gridIndex) => ({
          type: 'category',
          data: times,
          gridIndex,
          axisLine: { lineStyle: { color: splitColor } },
          axisLabel: gridIndex === 1 ? { show: false } : { color: textColor, hideOverlap: true },
          axisTick: { show: false },
        })),
        yAxis: (hasVolume ? [0, 1] : [0]).map((gridIndex) => ({
          scale: true,
          gridIndex,
          axisLabel: { color: textColor },
          splitLine: { lineStyle: { color: splitColor } },
        })),
        series: [
          {
            name: '价格',
            type: 'line',
            data: prices,
            showSymbol: false,
            lineStyle: { color: LINE_COLOR, width: 1.5 },
            itemStyle: { color: LINE_COLOR },
            areaStyle: { color: dark ? 'rgba(59,130,246,0.15)' : 'rgba(59,130,246,0.08)' },
          },
          ...(hasVolume
            ? [{
                name: '成交量',
                type: 'bar' as const,
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: volumeData,
              }]
            : []),
        ],
      },
      { notMerge: true },
    );
  }, [rows, theme]);

  return <div ref={containerRef} className="stock-chart" style={{ width: '100%', height: 260 }} />;
}
