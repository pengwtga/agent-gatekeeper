"""SSRM AIOps 全局配置

多 LLM Provider 容灾链：primary(deepseek/任意兼容) → zhipu(免费 glm-4-flash) → siliconflow(免费 Qwen) → 规则基线
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    app_name: str = "SSRM Ops Agent"
    debug: bool = False

    # 主 LLM（OpenAI 兼容接口，DeepSeek 或任意中转站）
    llm_api_key: Optional[str] = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # 备用 Provider：智谱（glm-4-flash 永久免费）
    zhipu_api_key: Optional[str] = None
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-4-flash"

    # 备用 Provider：硅基流动（Qwen2.5-7B-Instruct 免费档）
    siliconflow_api_key: Optional[str] = None
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model: str = "Qwen/Qwen2.5-7B-Instruct"

    # LLM 通用
    llm_temperature: float = 0.0
    llm_timeout_seconds: int = 30  # 无预算时的兜底读超时（评测脚本/离线任务）
    llm_max_tokens: int = 512  # 输出长度上界：根因 JSON 只需 ~150 token，512 足够且能防跑飞
    llm_max_retries: int = 1  # 单 Provider 内重试次数（跨 Provider 容灾由链路负责）

    # 请求级超时预算（T1-1）：单请求总时长上界，nginx 超时（20s）应大于它
    # 最坏路径 = 3 provider × 3s × 2 次 ≈ 18s 超 12s 预算 → 预算会掐掉尾部的 provider，
    # 这是有意的：宁可降级规则基线，也不让请求超过 nginx 的 504 线
    request_budget_ms: int = 12000
    llm_timeout_per_try_ms: int = 3000  # 单 Provider 单次尝试的时间片

    # RAG 值班助手
    embedding_model: str = "embedding-2"  # 智谱向量化模型
    rag_top_k: int = 3
    rag_min_score: float = 0.55  # 相似度低于此值的检索结果不采信
    warmup_timeout_seconds: int = 10  # 建索引不能无限期挡住启动，超时先以关键词模式接请求

    # 分类 Agent
    min_confidence_for_auto_action: float = 0.85  # 置信度低于此值 → 建议人工确认
    min_evidence_score: float = 0.35  # 【兜底判据】字面匹配可回溯度低于此值 → 无据（阈值标定见 docs/optimization/T0-03）
    # 【主判据】结构化引用（T3-5）：有效引用占比 + 引用条数双条件
    # 为什么还要条数下限：只引 1 个正确字段就能拿 1.0 分，等于奖励「少说少错」，
    # 这正是 T0-03 里分数反转的根源（J 极简 1.000 > A 详述 0.684）。条数下限把这条路堵掉。
    min_evidence_refs_score: float = 0.7  # 标定见 docs/optimization/T3-02：诚实样本 1.0 / 纯编造 0.0
    min_evidence_refs: int = 2
    low_evidence_confidence_cap: float = 0.4  # 无据时的置信度上限

    # Prompt 版本化（T3-1）：留空用仓库内 prompts/，可指向外部目录做 A/B 或多环境部署
    prompts_dir: str = ""
    # A/B 或紧急回滚：{"root_cause": "v1"} 覆盖 index.json 的 active 指针，不改仓库文件
    prompts_active: dict[str, str] = {}

    # 服务
    api_port: int = 8001

    # 可观测（T2）
    log_format: str = "json"  # json | text；本地调试想看人话就设 text
    log_level: str = "INFO"
    metrics_window_seconds: int = 900  # 延迟分位数/成功率的滑动窗口（15 分钟）
    metrics_max_samples: int = 2000  # 窗口内最多保留的延迟样本数（防内存增长）

    # 公网防护：/eval 一次请求会循环调用 LLM 30 次，需要 token 防刷（留空则 /eval 直接拒绝）
    eval_token: str = ""

    model_config = {"env_prefix": "SSRM_AIOPS_", "env_file": ".env"}


settings = Settings()
