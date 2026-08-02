/** 通用 UI：页面头部与空状态。 */

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty-state" data-testid="empty-state">
      <div className="empty-state-icon" aria-hidden="true">
        ◌
      </div>
      <h3 style={{ marginBottom: 8 }}>{title}</h3>
      <p style={{ margin: 0 }}>{hint ?? '暂无数据；系统不会展示虚构内容。'}</p>
    </div>
  );
}

export function PageHeader({ title, description }: { title: string; description?: string }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <h2 style={{ fontSize: 'var(--font-size-xl)', marginBottom: 4 }}>{title}</h2>
      {description && (
        <p style={{ margin: 0, color: 'var(--color-text-secondary)', fontSize: 14 }}>{description}</p>
      )}
    </div>
  );
}
