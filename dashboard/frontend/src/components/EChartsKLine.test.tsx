import { describe, expect, it } from 'vitest';
import { mapTradeMarkers } from '../components/EChartsKLine';
import type { StockBar, StockOrderItem } from '../api/client';

const BARS: StockBar[] = [
  { date: '2026-07-30', open: '1323.00', high: '1362.00', low: '1322.00', close: '1361.76', volume: 1, amount: 1 },
  { date: '2026-07-31', open: '1330.03', high: '1355.72', low: '1325.77', close: '1350.60', volume: 1, amount: 1 },
];

describe('mapTradeMarkers（买卖点映射）', () => {
  it('fill_date 存在时用 fill_date 且映射到真实 K 线日期', () => {
    const orders: StockOrderItem[] = [
      { signal_date: '2026-07-30', fill_date: '2026-07-31', symbol: '600519.SH', side: 'BUY', quantity: 100, status: 'FILLED', fill_price: '1350.00', reason: '' },
    ];
    const markers = mapTradeMarkers(BARS, orders);
    expect(markers).toHaveLength(1);
    expect(markers[0].date).toBe('2026-07-31');
    expect(markers[0].side).toBe('BUY');
    expect(markers[0].price).toBe(1350.0); // 用 fill_price
  });

  it('无 fill_date 时用 signal_date', () => {
    const orders: StockOrderItem[] = [
      { signal_date: '2026-07-30', fill_date: null, symbol: '600519.SH', side: 'SELL', quantity: 50, status: 'PENDING', fill_price: null, reason: '' },
    ];
    const markers = mapTradeMarkers(BARS, orders);
    expect(markers).toHaveLength(1);
    expect(markers[0].date).toBe('2026-07-30');
    expect(markers[0].price).toBe(1361.76); // 无 fill_price 用当日收盘
  });

  it('找不到对应 K 线日期时丢弃，不得猜测位置', () => {
    const orders: StockOrderItem[] = [
      { signal_date: '2025-01-01', fill_date: null, symbol: '600519.SH', side: 'BUY', quantity: 10, status: 'PENDING', fill_price: null, reason: '' },
    ];
    expect(mapTradeMarkers(BARS, orders)).toHaveLength(0);
  });

  it('非 BUY/SELL 的订单被忽略', () => {
    const orders: StockOrderItem[] = [
      { signal_date: '2026-07-31', fill_date: '2026-07-31', symbol: '600519.SH', side: 'HOLD', quantity: 10, status: 'PENDING', fill_price: null, reason: '' },
    ];
    expect(mapTradeMarkers(BARS, orders)).toHaveLength(0);
  });
});
