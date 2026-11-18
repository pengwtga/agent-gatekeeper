"""把真实依赖装配起来（CLI / MCP server / 进程内调用共用）。

这里只做「组装」，不含任何流程逻辑——所以它可以在测试里被整个替换掉：
`main(argv, deps_factory=fake_factory)`。
"""

import os

from oncall.api import Deps


class ConfigError(RuntimeError):
    """配置缺失（Key 不存在、领域未知）——CLI 映射成退出码 4。"""


_DOMAIN_NAMES = ("ssrm", "csa", "credit")
DEFAULT_DB_PATH = "data/oncall.sqlite3"
DEFAULT_MEMORY_PATH = "data/incident_memory.sqlite3"


def build_store() -> "RunStore":  # noqa: F821 —— 字符串注解避免顶部循环导入
    from oncall.store import RunStore

    return RunStore(os.environ.get("ONCALL_DB") or DEFAULT_DB_PATH).init()


def build_memory():
    """事故记忆（F15）。ONCALL_MEMORY 可覆盖路径；空串 = 显式关闭记忆。"""
    from oncall.memory import IncidentMemory

    path = os.environ.get("ONCALL_MEMORY", DEFAULT_MEMORY_PATH)
    return None if path == "" else IncidentMemory(path)


async def build_registry(domain: str = "ssrm", *, require_llm: bool = True):
    """构建领域注册表（CLI 与 MCP server 共用）。

    require_llm=False 给 serve 用：MCP 暴露的都是只读工具，检索在无 LLM 时
    自动降级为关键词序——server 不应该因为宿主机没配 Key 就起不来。
    """
    from app.agents.rag_assistant import RagAssistant
    from app.core.builtin_tools import build_default_registry
    from app.core.llm_client import llm_client
    from oncall.rag_loop import register_rag_tool

    if domain not in _DOMAIN_NAMES:
        raise ConfigError(f"未知领域 '{domain}'，可选：{', '.join(_DOMAIN_NAMES)}")

    if require_llm and not llm_client.is_configured:
        raise ConfigError(
            "没有可用的 LLM Provider：请在 .env 配置 ZHIPU_API_KEY 或 SILICONFLOW_API_KEY"
        )

    if domain != "ssrm":
        # builtin_tools 是 ssrm 的知识面（工单/拓扑），其他域暂无工具集。
        # 如实拒绝，而不是给一个空注册表——空表会让模型每步都「没有合适工具」，
        # 看起来像模型不行，其实是配置问题。
        raise ConfigError(f"领域 '{domain}' 暂未提供工具集（目前只接 ssrm）")

    from app.domains.ssrm import pack as ssrm_pack

    rag = RagAssistant(llm_client, ssrm_pack.assistant)
    await rag.warmup()
    registry = build_default_registry(rag)
    register_rag_tool(registry, rag)  # F12：检索升级为「自评+改写」的只读工具
    return registry


async def build_serve_registry(domain: str = "ssrm"):
    """MCP server 的注册表：不要求 LLM 配置（F09 §5——server 只做读）。"""
    return await build_registry(domain, require_llm=False)


def build_skill_book():
    """技能库（F10）。ONCALL_SKILLS 可覆盖目录；空串 = 显式关闭。"""
    from oncall.skills_loader import build_skill_book as _build

    path = os.environ.get("ONCALL_SKILLS", "skills")
    return None if path == "" else _build(path)


async def build_default_deps(domain: str = "ssrm") -> Deps:
    from app.core.llm_client import llm_client
    from oncall.skills_loader import register_skill_tool

    registry = await build_registry(domain)
    skills = build_skill_book()
    if skills is not None:
        register_skill_tool(registry, skills)  # F10：显式加载通道（只读取文本）
    return Deps(llm=llm_client, registry=registry, store=build_store(),
                memory=build_memory(), skills=skills)
