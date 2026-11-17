"""把真实依赖装配起来（CLI / 进程内调用共用）。

这里只做「组装」，不含任何流程逻辑——所以它可以在测试里被整个替换掉：
`main(argv, deps_factory=fake_factory)`。
"""

from oncall.api import Deps


class ConfigError(RuntimeError):
    """配置缺失（Key 不存在、领域未知）——CLI 映射成退出码 4。"""


_DOMAIN_NAMES = ("ssrm", "csa", "credit")


async def build_default_deps(domain: str = "ssrm") -> Deps:
    from app.agents.rag_assistant import RagAssistant
    from app.core.builtin_tools import build_default_registry
    from app.core.llm_client import llm_client

    if domain not in _DOMAIN_NAMES:
        raise ConfigError(f"未知领域 '{domain}'，可选：{', '.join(_DOMAIN_NAMES)}")

    if not llm_client.is_configured:
        raise ConfigError(
            "没有可用的 LLM Provider：请在 .env 配置 ZHIPU_API_KEY 或 SILICONFLOW_API_KEY"
        )

    if domain != "ssrm":
        # builtin_tools 是 ssrm 的知识面（工单/拓扑），其他域暂无工具集。
        # 如实拒绝，而不是给一个空注册表——空表会让模型每步都「没有合适工具」，
        # 看起来像模型不行，其实是配置问题。
        raise ConfigError(f"领域 '{domain}' 暂未提供工具集（I1 只接 ssrm）")

    from app.domains.ssrm import pack as ssrm_pack

    rag = RagAssistant(llm_client, ssrm_pack.assistant)
    await rag.warmup()
    return Deps(llm=llm_client, registry=build_default_registry(rag))
