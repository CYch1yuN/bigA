import { describe, expect, it, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithProviders } from '../test/render';
import { PredictionSummaryCard } from './PredictionSummaryCard';
import { api } from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    api: {
      predictionSummary: vi.fn(),
    },
  };
});

const FRESH = {
  ok: true,
  schema_version: 1,
  source: 'biga-evaluation',
  as_of: '2026-08-08T09:00:00+08:00',
  fetched_at: '2026-08-08T10:00:00+08:00',
  cache_status: 'fresh',
  is_realtime: false,
  transport: 'local_evaluation',
  availability: 'fresh',
  data: {
    model_version: 'v1.0',
    task_name: '未来 5 个交易日上涨 >=2%',
    horizon_days: 5,
    target_return: 0.02,
    accuracy: 0.85,
    precision: 0.75,
    recall: 0.6,
    auc: 0.61,
    sample_count: 1200,
    test_start: '2026-01-01',
    test_end: '2026-06-30',
    net_return: 0.08,
    max_drawdown: -0.12,
    sharpe: 1.2,
    benchmark_return: 0.03,
    gate_status: 'passed',
    gate_version: 'gate-v1',
    gate_reasons: [],
  },
  warnings: [],
} as const;

const JUNK = ['NaN', 'Infinity', 'undefined', '[object Object]'];

describe('PredictionSummaryCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('fresh：展示已通过徽标、样本外准确率、样本数与测试区间', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue(FRESH);
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('已通过')).toBeTruthy());
    expect(screen.getByText('样本外准确率')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('85.0%')).toBeTruthy());
    expect(screen.getByText(/样本 1200 个/)).toBeTruthy();
    expect(screen.getByText(/2026-01-01 ~ 2026-06-30/)).toBeTruthy();
    expect(screen.getByText(/未来 5 个交易日上涨 >=2%/)).toBeTruthy();
    const body = screen.getByText('预测有效性').parentElement?.textContent ?? '';
    expect(body).toContain('Precision');
    expect(body).toContain('Recall');
  });

  it('比例字段复用 ×100 百分比格式：0.08→8.00%、-0.12→-12.00%、0.03→3.00%', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue(FRESH);
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('已通过')).toBeTruthy());
    const body = screen.getByText('预测有效性').parentElement?.textContent ?? '';
    expect(body).toContain('扣费后收益：8.00%');
    expect(body).toContain('最大回撤：-12.00%');
    expect(body).toContain('基准收益：3.00%');
    // 不得出现未乘 100 的原始小数拼接
    expect(body).not.toContain('扣费后收益：0.08%');
    expect(body).not.toContain('最大回撤：-0.12%');
  });

  it('55% accuracy 不能显示已通过；显示门槛原因中文', async () => {
    const notPassed = {
      ...FRESH,
      data: {
        ...FRESH.data,
        accuracy: 0.55,
        gate_status: 'not_passed',
        gate_reasons: ['样本外准确率 0.55 < 0.80', 'Precision 0.60 < 0.70', '扣费后收益未超过基准（或缺失）'],
      },
    };
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue(notPassed);
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('未达到门槛')).toBeTruthy());
    expect(screen.queryByText('已通过')).toBeNull();
    expect(screen.getByText('未通过门槛：')).toBeTruthy();
    expect(screen.getByText('样本外准确率 0.55 < 0.80')).toBeTruthy();
    expect(screen.getByText('Precision 0.60 < 0.70')).toBeTruthy();
    expect(screen.getByText('扣费后收益未超过基准（或缺失）')).toBeTruthy();
    expect(screen.getByText(/门槛版本：gate-v1/)).toBeTruthy();
    const body = document.body.textContent ?? '';
    for (const j of JUNK) {
      expect(body).not.toContain(j);
    }
  });

  it('fresh：不显示任何交易按钮与 NaN 类文本', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue(FRESH);
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('预测有效性')).toBeTruthy());
    const body = document.body.textContent ?? '';
    for (const w of ['买入', '卖出', '下单', '自动交易']) {
      expect(body).not.toContain(w);
    }
    for (const j of JUNK) {
      expect(body).not.toContain(j);
    }
  });

  it('unavailable：展示暂无评估结果空态并保留布局', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      schema_version: 1,
      source: 'biga-evaluation',
      as_of: null,
      fetched_at: '2026-08-08T00:00:00+08:00',
      cache_status: 'unavailable',
      is_realtime: false,
      transport: 'local_evaluation',
      availability: 'unavailable',
      data: null,
      warnings: ['暂无经过严格样本外验证的预测准确率'],
    });
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('暂无评估结果')).toBeTruthy());
    expect(screen.getByText(/暂无经过严格样本外验证的预测准确率/)).toBeTruthy();
    const body = document.body.textContent ?? '';
    for (const j of JUNK) {
      expect(body).not.toContain(j);
    }
  });

  it('样本不足：warning 含样本数不足时展示样本不足徽标', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      schema_version: 1,
      source: 'biga-evaluation',
      as_of: null,
      fetched_at: '2026-08-08T00:00:00+08:00',
      cache_status: 'unavailable',
      is_realtime: false,
      transport: 'local_evaluation',
      availability: 'unavailable',
      data: null,
      warnings: ['样本数不足（10<30）；暂无经过严格样本外验证的预测准确率'],
    });
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('样本不足')).toBeTruthy());
  });

  it('stale：展示评估结果已过期提示，数据仍保留', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...FRESH,
      availability: 'stale',
      cache_status: 'stale',
      as_of: '2026-07-01T10:00:00+08:00',
    });
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('评估结果已过期')).toBeTruthy());
    expect(screen.getByText('85.0%')).toBeTruthy();
    const body = document.body.textContent ?? '';
    for (const j of JUNK) {
      expect(body).not.toContain(j);
    }
  });

  it('unavailable：gate_status=insufficient_data 时展示样本不足徽标（有数据但样本不够）', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...FRESH,
      data: { ...FRESH.data, sample_count: 10, gate_status: 'insufficient_data' },
    });
    renderWithProviders(<PredictionSummaryCard />);
    await waitFor(() => expect(screen.getByText('样本不足')).toBeTruthy());
  });

  it('移动端窄容器（390px）渲染正常且无异常文本', async () => {
    (api.predictionSummary as ReturnType<typeof vi.fn>).mockResolvedValue(FRESH);
    renderWithProviders(
      <div style={{ width: 390 }}>
        <PredictionSummaryCard />
      </div>,
    );
    await waitFor(() => expect(screen.getByText('预测有效性')).toBeTruthy());
    const body = document.body.textContent ?? '';
    for (const j of JUNK) {
      expect(body).not.toContain(j);
    }
  });
});
