import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api, WestockCapability } from '../api/client';
import { PageHeader } from '../components/ui';
import { formatTime, Metric } from '../dashboard/data';

const STATUS_TEXT: Record<string, string> = {
  fresh: '新鲜', stale: '已过期', unavailable: '无缓存', unsupported: '不支持',
};

function statusTone(status: string) {
  if (status === 'fresh') return 'success';
  if (status === 'stale') return 'warning';
  return 'neutral';
}

function ageText(seconds: number | null) {
  if (seconds == null) return '暂无';
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时`;
}

export function WestockConnectionPage() {
  const client = useQueryClient();
  const [notice, setNotice] = useState('');
  const query = useQuery({
    queryKey: ['westock-connection'], queryFn: api.westockConnection, refetchInterval: 60_000,
  });
  const refresh = useMutation({
    mutationFn: (capabilities: string[]) => api.westockRefresh(capabilities),
    onSuccess: async (result) => {
      setNotice(result.message);
      await client.invalidateQueries({ queryKey: ['westock-connection'] });
    },
    onError: () => setNotice('刷新请求失败；现有缓存和本地研究数据不受影响。'),
  });
  const groups = useMemo(() => {
    const result = new Map<string, WestockCapability[]>();
    for (const item of query.data?.data.capabilities ?? []) {
      result.set(item.group, [...(result.get(item.group) ?? []), item]);
    }
    return [...result.entries()];
  }, [query.data]);

  if (query.isLoading) return <div className="card loading-state">正在读取 Westock 连接状态…</div>;
  if (query.isError || !query.data) {
    return <div className="alert alert-error">无法读取 Westock 连接状态；本地研究主链不受影响。</div>;
  }
  const status = query.data;
  return (
    <div>
      <PageHeader title="Westock 数据连接" description="腾讯自选股研究数据旁路 · 不写入回测与模拟账户主链" />
      <div className="metric-grid">
        <Metric label="MCP 连接" value={status.availability.connected ? '已连接' : '未连接'} hint={status.transport === 'direct_mcp' ? 'MCP 直连' : '缓存导出桥'} tone={status.availability.connected ? 'success' : 'neutral'} />
        <Metric label="缓存可用" value={status.availability.cache_available ? '可用' : '不可用'} hint={status.cache_status === 'fresh' ? '含新鲜缓存' : status.cache_status === 'stale' ? '仅过期缓存' : '暂无缓存数据'} tone={status.availability.cache_available ? 'success' : 'warning'} />
        <Metric label="已缓存能力" value={status.data.fresh_count + status.data.stale_count} hint={`共 ${status.data.capability_count} 项`} />
        <Metric label="新鲜缓存" value={status.data.fresh_count} tone={status.data.fresh_count ? 'success' : 'neutral'} />
      </div>

      <div className="card section-card connection-summary">
        <div>
          <div className="card-title compact">连接说明</div>
          <p className="body-copy">Dashboard 独立进程当前没有 WorkBuddy Connector 的授权上下文，因此使用标准化缓存导出桥——MCP 连接为「未连接」，缓存可用性单独表达。页面不会把缓存数据标记为实时，也不会因 Westock 不可用而阻断本地研究。</p>
        </div>
        <button className="btn" disabled={refresh.isPending} onClick={() => refresh.mutate([])}>{refresh.isPending ? '请求中…' : '刷新连接状态'}</button>
      </div>
      {notice && <div className="alert alert-success" role="status">{notice}</div>}
      {status.warnings.map((warning) => <div className="alert alert-warning" key={warning}>{warning}</div>)}

      <div className="connection-groups">
        {groups.map(([group, capabilities]) => (
          <section className="card connection-group" key={group}>
            <div className="connection-group-header">
              <div><div className="card-title compact">{group}</div><div className="muted">{capabilities.length} 项只读研究能力</div></div>
              <button className="btn" disabled={refresh.isPending} onClick={() => refresh.mutate(capabilities.map((item) => item.name))}>刷新本组</button>
            </div>
            <div className="table-wrap">
              <table className="table connection-table">
                <thead><tr><th>能力</th><th>MCP 工具</th><th>状态</th><th>缓存年龄</th><th>最近成功</th><th>熔断</th></tr></thead>
                <tbody>{capabilities.map((item) => (
                  <tr key={item.name}>
                    <td><strong>{item.name}</strong></td><td><code>{item.tool}</code></td>
                    <td><span className={`badge badge-${statusTone(item.status)}`}>{STATUS_TEXT[item.status] ?? item.status}</span></td>
                    <td>{ageText(item.cache_age_seconds)}</td><td>{formatTime(item.last_success_at)}</td>
                    <td>{item.circuit_state === 'closed' ? '正常' : '未观测'}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </section>
        ))}
      </div>

      <div className="card section-card">
        <div className="card-title">数据边界</div>
        <ul className="connection-boundaries">
          <li>本地 curated 是历史行情、复权、指标计算和回测的唯一权威来源。</li>
          <li>Westock 数据只用于研究展示、资讯补充与交叉核验。</li>
          <li>Westock qfq/hfq 当前不可信，不进入 K 线、策略、订单或 Gate 4B。</li>
          <li>不接入 Westock 模拟交易；BigA 模拟账户保持唯一账本。</li>
        </ul>
      </div>
    </div>
  );
}
