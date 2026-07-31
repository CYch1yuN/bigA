"""Phase 2 事件驱动回测器包。

核心组件：
- models: 数据模型（Signal, Order, Fill, Position, PortfolioSnapshot, BacktestResult）
- config: 回测配置（BacktestConfig）
- interfaces: 抽象接口（Strategy, UniverseFilter, RiskManager, BrokerSimulator）
- broker: A股成交模拟器
- risk: 默认风控管理器
- universe: 默认股票池过滤器
- engine: 回测引擎
- metrics: 绩效指标计算
- report: JSON/Markdown 报告生成
"""
