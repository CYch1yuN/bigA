import { useAuth } from '../auth/AuthContext';
import { EmptyState, PageHeader } from '../components/ui';
import { DataBoundary, Metric, StatusBadge, money, useDashboardSnapshot } from '../dashboard/data';

export { OverviewPage } from './OverviewPage';

export function Gate4BPage() {
  const query = useDashboardSnapshot();
  return (
    <div>
      <PageHeader title="Gate 4B" description="连续运行观测 · 只读展示" />
      <DataBoundary query={query} available={(data) => Boolean(data.gate4b)} emptyTitle="Gate 4B 观测数据">
        {(data) => {
          const gate = data.gate4b;
          const progress = Number(gate.observation_progress ?? gate.real_success_trading_days ?? 0);
          const target = Number(gate.observation_target ?? 60);
          const pct = target > 0 ? Math.min(100, progress / target * 100) : 0;
          return <>
            <div className="metric-grid">
              <Metric label="观察进度" value={`${progress}/${target}`} hint={`${pct.toFixed(1)}%`} />
              <Metric label="连续交易日" value={gate.consecutive_trading_days ?? 0} />
              <Metric label="违规项" value={(gate.violations ?? []).length} tone={(gate.violations ?? []).length ? 'danger' : 'success'} />
              <Metric label="数据模式" value={gate.online ? '在线' : gate.synthetic ? '合成验证' : '尚未在线'} />
            </div>
            <div className="card section-card">
              <div className="progress-label"><span>60 日观察窗口</span><span>{pct.toFixed(1)}%</span></div>
              <div className="progress-track"><div className="progress-fill" style={{ width: `${pct}%` }} /></div>
              <p className="body-copy">{gate.disclaimer}</p>
            </div>
          </>;
        }}
      </DataBoundary>
    </div>
  );
}

export function SimAccountPage() {
  const query = useDashboardSnapshot();
  return (
    <div>
      <PageHeader title="模拟账户" description="仅用于研究信号与模拟账户，不连接券商，不涉及真实资金" />
      <DataBoundary query={query} available={(data) => Boolean(data.accounts)} emptyTitle="模拟账户">
        {(data) => <div className="account-grid">{(data.accounts?.accounts ?? []).map((account: any) => {
          const equity = data.accounts?.equity?.[account.account_id] ?? {};
          return <div className="card" key={account.account_id}>
            <div className="card-row"><div><div className="card-title compact">{account.account_id}</div><div className="muted">{account.strategy_track === 'steady' ? '稳健轨' : '激进轨'}</div></div><span className="badge badge-neutral">{account.eligibility_status}</span></div>
            <div className="account-values"><div><span>总权益</span><strong>{money(equity.total_equity ?? account.cash)}</strong></div><div><span>现金</span><strong>{money(account.cash)}</strong></div><div><span>持仓</span><strong>{account.positions?.length ?? 0}</strong></div></div>
            <div className="muted top-gap">截至 {account.as_of_date} · 观察 {account.observation_days ?? 0} 个交易日</div>
          </div>;
        })}</div>}
      </DataBoundary>
    </div>
  );
}

export function SignalsPage() {
  const query = useDashboardSnapshot();
  return (
    <div>
      <PageHeader title="信号与订单" description="研究信号与模拟订单 · 非交易指令" />
      <DataBoundary query={query} available={(data) => Boolean(data.signals || data.orders)} emptyTitle="信号与订单">
        {(data) => {
          const signals = data.signals?.signals ?? [];
          const orders = data.orders?.orders ?? [];
          return <><div className="metric-grid"><Metric label="研究信号" value={signals.length} /><Metric label="模拟订单" value={orders.length} /><Metric label="产物日期" value={data.artifact_date ?? '暂无'} /></div>
          <div className="card section-card table-wrap"><div className="card-title">最新研究信号</div>{signals.length ? <table className="table"><thead><tr><th>日期</th><th>轨道</th><th>代码</th><th>方向</th><th>数量</th><th>原因</th></tr></thead><tbody>{signals.map((item: any, index: number) => <tr key={item.signal_hash ?? index}><td>{item.signal_date}</td><td>{item.track}</td><td>{item.symbol}</td><td><span className={`badge ${item.side === 'BUY' ? 'badge-success' : 'badge-warning'}`}>{item.side}</span></td><td>{item.quantity}</td><td>{item.reason}</td></tr>)}</tbody></table> : <EmptyState title="本期无新增信号" hint="没有信号不代表系统异常。" />}</div></>;
        }}
      </DataBoundary>
    </div>
  );
}

export function DataQualityPage() {
  const query = useDashboardSnapshot();
  return (
    <div>
      <PageHeader title="数据质量" description="最近一次自动化质量闸门摘要" />
      <DataBoundary query={query} available={(data) => Boolean(data.quality)} emptyTitle="数据质量">
        {(data) => { const q = data.quality; const s = q.summary ?? {}; return <><div className="metric-grid"><Metric label="严重问题" value={s.critical ?? 0} tone={s.critical ? 'danger' : 'success'} /><Metric label="警告" value={s.warning ?? 0} tone={s.warning ? 'warning' : 'success'} /><Metric label="检查行数" value={s.rows_checked ?? 0} /><Metric label="质量闸门" value={q.has_critical ? '阻断' : '通过'} tone={q.has_critical ? 'danger' : 'success'} /></div><div className="card section-card"><div className="card-title">质量结论</div><p className="body-copy">共发现 {q.issues_count ?? 0} 项问题。严重问题会阻断主研究流程；westock 旁路异常只产生告警。</p></div></>; }}
      </DataBoundary>
    </div>
  );
}

export function RunHistoryPage() {
  const query = useDashboardSnapshot();
  return (
    <div>
      <PageHeader title="运行记录" description="每日与每周自动化任务的只读历史" />
      <DataBoundary query={query} available={(data) => data.run_history.length > 0} emptyTitle="运行记录">
        {(data) => <div className="card table-wrap"><table className="table"><thead><tr><th>业务日期</th><th>任务</th><th>状态</th><th>耗时</th><th>说明</th></tr></thead><tbody>{data.run_history.map((run: any) => <tr key={run.run_id}><td>{run.as_of_date}</td><td>{run.task_type}</td><td><StatusBadge state={run.state} /></td><td>{Number(run.duration_seconds ?? 0).toFixed(2)}s</td><td>{run.message}</td></tr>)}</tbody></table></div>}
      </DataBoundary>
    </div>
  );
}

export function SettingsPage() {
  const { safety } = useAuth();

  return (
    <div>
      <PageHeader title="系统设置" description="Dashboard 安全设置" />
      <div className="card">
        <div className="card-title">安全设置</div>
        <div className="badge badge-success">
          <span className="dot dot-success" aria-hidden="true" /> 安全边界已生效
        </div>
        <ul style={{ color: 'var(--color-text-secondary)', fontSize: 14, lineHeight: 1.8, paddingLeft: 20 }}>
          <li>实时交易：未启用</li>
          <li>券商连接：未连接</li>
          <li>会话 Cookie：HttpOnly + SameSite=Strict</li>
          <li>写操作需 CSRF 校验</li>
          <li>登录失败限流保护</li>
          <li>作业执行：固定 argv 白名单 + 无 shell + 超时 + 输出截断</li>
          <li>写入型作业串行执行，防止并发修改状态与报告</li>
        </ul>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-title">本地操作</div>
        <p style={{ color: 'var(--color-text-secondary)', fontSize: 14, marginTop: 0 }}>
          操作入口位于首页「操作中心」：环境检查、运行每日/每周任务、日期区间补跑与失败重跑，
          均通过本地自动化 CLI 真实执行，仅作用于模拟账户。
        </p>
        <p style={{ color: 'var(--color-text-secondary)', fontSize: 14 }}>
          允许的作业类型：{(safety?.allowed_actions ?? []).join('、')}。
        </p>
      </div>

      <div style={{ marginTop: 16 }}>
        <EmptyState title="密码修改" hint="后端接口已具备；表单将在后续设置页迭代中开放。" />
      </div>
    </div>
  );
}
