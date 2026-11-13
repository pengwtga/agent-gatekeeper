"""Prompt 版本化加载（T3-1）

prompt 是行为的一部分 —— 改一个字，所有样本的输出都可能变。
所以它不该散在代码 f-string 里：那样「改之前那版是不是更好」只能靠记忆回答。

本模块把 prompt 收敛到 `prompts/` 目录：

- `prompts/index.json` 是唯一入口：每个 prompt 名 → `active` 版本 + 版本表（`versions`）
- 每个版本一个文件，文件名 `<name>.<version>.md`
- 版本表声明 `vars`（模板占位符清单）。启动时校验「声明的 vars == 文本里实际的占位符」：
  改了模板忘了同步声明，会在启动时直接失败，而不是运行到某个分支才 KeyError

设计取舍（面试可讲）：
1. **不用 Jinja2**：prompt 里只有平铺变量，`str.format` 够用，少一个依赖、少一层黑盒
2. **校验放在启动（lifespan）而不是 import**：单测 import 模块不该因为 prompts/ 缺席就炸；
   但服务启动必须 fail fast —— 带着错的 prompt 跑起来比起不来更糟
3. **版本随结果一起返回**（`prompt_version` 字段）：一次线上问题可以反推到「当时用的是哪版 prompt」
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 进程内缓存：{name: Prompt}。版本切换需要重启（或测试里 reload），
# 不追求热更新——prompt 热更新等于线上行为可被外部文件悄然改变，不可审计
_CACHE: dict[str, "Prompt"] = {}


class PromptError(RuntimeError):
    """prompt 配置错误：启动即失败，不允许带病运行"""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str
    vars: tuple[str, ...] = ()
    # 追加在 few-shot 之后的输出形态示例（T3-5）。为什么需要它：
    # 领域包里的 few-shot 是「输入 + 标准答案」，属于领域知识；而输出 schema 属于 prompt 版本。
    # 大模型对齐的是最后看到的格式，所以声明式地把 schema 放在示例之后，
    # 避免「few-shot 还是老 schema、正文要求新 schema」的自相矛盾。
    post_example: str = ""

    @property
    def label(self) -> str:
        """日志/评测输出用的短标识：root_cause.v1"""
        return f"{self.name}.{self.version}"

    def render(self, **kwargs: Any) -> str:
        missing = [v for v in self.vars if v not in kwargs]
        if missing:
            raise PromptError(f"{self.label} 渲染缺少变量: {missing}")
        try:
            return self.text.format(**kwargs)
        except (KeyError, IndexError) as e:
            raise PromptError(f"{self.label} 渲染失败（模板与入参不符）: {e!r}") from e


def _prompts_dir() -> Path:
    """prompts/ 目录。settings.prompts_dir 可覆盖（测试 / 多环境部署）"""
    override = settings.prompts_dir.strip()
    return Path(override) if override else _PROJECT_ROOT / "prompts"


def _read_index() -> dict[str, Any]:
    path = _prompts_dir() / "index.json"
    if not path.exists():
        raise PromptError(f"prompt 索引不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PromptError(f"prompt 索引不是合法 JSON: {path}: {e}") from e


def _placeholders(text: str) -> set[str]:
    """提取模板里真正的占位符（`{{...}}` 是转义字面量，不算）"""
    return {field for _, field, _, _ in Formatter().parse(text) if field}


def _build(name: str, index: dict[str, Any]) -> "Prompt":
    entry = index.get(name)
    if entry is None:
        raise PromptError(f"prompt 未登记: {name}（可选: {sorted(index)}）")
    # A/B 与回滚：settings.prompts_active 可覆盖 index 的 active 指针（不动仓库文件就能对比两版）
    override = (settings.prompts_active or {}).get(name)
    active = override or entry.get("active")
    versions = entry.get("versions") or {}
    if not active or active not in versions:
        raise PromptError(f"{name}: active={active!r} 不在版本表里（已有: {sorted(versions)}）")

    meta = versions[active]
    file_path = _prompts_dir() / meta["file"]
    if not file_path.exists():
        raise PromptError(f"{name}.{active}: 文件不存在: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    # md 文件天然以换行结尾，旧代码里的 f-string 没有。去掉这一个行尾换行，
    # 保证「外置」是纯搬运而非改行为 —— 这条可以用回归集与 diff 双重验证
    if text.endswith("\n"):
        text = text[:-1]
    declared = tuple(meta.get("vars") or [])
    actual = _placeholders(text)
    if set(declared) != actual:
        raise PromptError(
            f"{name}.{active}: 占位符与声明不一致 —— 声明 {sorted(declared)} / 实际 {sorted(actual)}"
        )
    # post_example 原样追加、不过 format（里面是 JSON 字面量，花括号不该被模板引擎吃），
    # 所以这里不做占位符校验 —— 否则示例里的 {"field": ...} 会被当成未声明变量误报
    post = str(meta.get("post_example") or "")
    return Prompt(name=name, version=active, text=text, vars=declared, post_example=post)


def load(name: str) -> Prompt:
    """按 active 指针加载 prompt（带缓存）。配置错误抛 PromptError"""
    if name not in _CACHE:
        _CACHE[name] = _build(name, _read_index())
    return _CACHE[name]


def validate() -> dict[str, str]:
    """启动校验：索引结构 / active 存在 / 文件存在 / 占位符一致。返回 {name: version}"""
    index = _read_index()
    result: dict[str, str] = {}
    for name in index:
        prompt = _build(name, index)
        _CACHE[name] = prompt
        result[name] = prompt.version
    return result


def active_versions() -> dict[str, str]:
    """已加载的 prompt 版本快照，供 /health 暴露"""
    return {name: p.version for name, p in sorted(_CACHE.items())}


def reload() -> dict[str, str]:
    """清缓存并重新校验（测试 / 运维手动重载）"""
    _CACHE.clear()
    versions = validate()
    logger.info("prompt 已重新加载", extra={"event": "prompts_reloaded", "stage": "boot",
                                            "reason": str(versions), "outcome": "ok"})
    return versions


def record_score(prompt_name: str, version: str, metric: str, score: float) -> Path:
    """把一次评测分数记到 prompts/scores.json（T3-3）：版本 × 指标 → 分数 + 时间戳。

    为什么不写回 index.json：索引是"声明"，分数是"运行时观测"。
    混在一起会让「切了个版本」这种一次性的变更，每次都夹带一堆数字 diff。
    """
    from datetime import datetime, timezone

    path = _prompts_dir() / "scores.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError:
        data = {}
    entry = data.setdefault(prompt_name, {}).setdefault(version, {})
    entry[metric] = {
        "score": round(score, 4),
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
