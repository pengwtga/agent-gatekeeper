"""依赖方向守卫：让「新包 vs 老包」的边界变成会红的测试，而不是口头约定。

三条规则：
1. oncall 不许 import app.agents.pipeline —— 那是 v1 的固定流程，也是对照组 A。
   一旦 v2 复用了它，A/B 对照就不再是「两种执行层」，而是「同一实现跑两遍」。
2. app 不许 import oncall —— v1 必须能独立运行、独立部署、独立回滚。
3. oncall 对 app 的 import 限定在白名单内 —— 复用的是「通用件」，不是「某个业务实现」。

用 AST 而不是正则：`from app.agents import pipeline` 和 `import app.agents.pipeline`
两种写法都要抓得到，且注释里的字符串不算。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# oncall 允许依赖的 app 子模块（通用件）
ALLOWED_APP_PREFIXES = (
    "app.core",
    "app.domains",
    "app.observability",
    "app.agents.rag_assistant",
    "app.config",
)

# 明确禁止：v1 的编排流程
FORBIDDEN_IN_ONCALL = ("app.agents.pipeline", "oncall")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
            # from app.agents import pipeline 也要算成 app.agents.pipeline
            mods.update(f"{node.module}.{alias.name}" for alias in node.names)
    return mods


def _scan(directory: Path) -> dict[Path, set[str]]:
    return {p: _imported_modules(p) for p in directory.rglob("*.py") if "__pycache__" not in p.parts}


def test_oncall_does_not_import_v1_pipeline():
    offenders = {
        path.relative_to(ROOT): sorted(m for m in mods if m in FORBIDDEN_IN_ONCALL[:1]
                                       or m.startswith("app.agents.pipeline"))
        for path, mods in _scan(ROOT / "oncall").items()
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"oncall 不许依赖 v1 的编排流程：{offenders}"


def test_oncall_only_imports_whitelisted_app_modules():
    bad: dict[str, list[str]] = {}
    for path, mods in _scan(ROOT / "oncall").items():
        outside = sorted(
            m for m in mods
            if m.startswith("app.") and not m.startswith(ALLOWED_APP_PREFIXES)
        )
        if outside:
            bad[str(path.relative_to(ROOT))] = outside
    assert not bad, f"oncall 依赖了白名单之外的 app 模块：{bad}"


def test_app_does_not_import_oncall():
    offenders = {
        str(path.relative_to(ROOT)): sorted(m for m in mods if m == "oncall" or m.startswith("oncall."))
        for path, mods in _scan(ROOT / "app").items()
    }
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"v1 必须能独立运行，不许反向依赖 oncall：{offenders}"
