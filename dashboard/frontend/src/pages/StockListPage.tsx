import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { PageHeader } from '../components/ui';

export function StockListPage() {
  const [query, setQuery] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const list = useQuery({
    queryKey: ['stocks-list', query],
    queryFn: () => api.stocksList({ query: query || undefined, limit: 100, offset: 0 }),
  });

  return (
    <div>
      <PageHeader title="股票行情" description="本地 curated 历史行情 · 研究旁路，不进入回测与模拟账户主链" />
      <div className="card section-card">
        <div className="stock-search-row">
          <input
            className="input"
            placeholder="搜索股票代码，如 600519 或 600519.SH"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') setQuery(searchInput.trim()); }}
          />
          <button className="btn" onClick={() => setQuery(searchInput.trim())}>搜索</button>
        </div>
      </div>

      {list.isLoading && <div className="card loading-state">正在读取本地标的…</div>}
      {list.isError && <div className="alert alert-error">无法读取股票列表；本地研究主链不受影响。</div>}
      {!list.isLoading && !list.isError && list.data && (
        <div className="card section-card">
          <div className="card-title compact">本地有历史行情的标的（{list.data.data.total}）</div>
          {list.data.data.items.length === 0 ? (
            <p className="body-copy">没有匹配的标的。仅展示本地 curated 确实存在历史行情的股票；未补跑的历史行情不会显示。</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>代码</th><th>名称</th><th>最新本地交易日</th><th>K 线数量</th><th>可用性</th><th /></tr></thead>
                <tbody>
                  {list.data.data.items.map((item) => (
                    <tr key={item.symbol}>
                      <td><strong>{item.symbol}</strong></td>
                      <td>{item.name ?? '—'}</td>
                      <td>{item.latest_trade_date}</td>
                      <td>{item.bar_count}</td>
                      <td><span className="badge badge-success">本地数据可用</span></td>
                      <td><Link className="btn btn-sm" to={`/stocks/${encodeURIComponent(item.symbol)}`}>查看</Link></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
