"""可观测：trace_id、span 链路、指标聚合、JSON 日志、访问中间件

- tracing   进程内 span 采集（埋点 → 瀑布图）
- metrics   指标聚合（分位数 / 分钟时序 / 请求流水 / 分类分布）
- obs       FastAPI 中间件（trace_id + 访问记录）与日志初始化
"""
