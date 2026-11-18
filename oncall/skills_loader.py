"""F10 Skill 运行时加载：扫描 → 索引 → 命中 → 注入。

为什么不是「把流程写进 SYSTEM_PROMPT」：
- prompt 常驻付全价：800 token 的流程段每一轮都在，哪怕这次根本用不上；
- 加一种打法要改代码发版；一个 prompt 塞不下第二个领域的打法。

渐进披露（这是 skill 相对长 prompt 的唯一结构性优势）：
- 常驻：每个 skill 一行 `name — description`（约 20 token）；
- 命中：正文才进上下文（隐式命中 = 关键词触发；显式命中 = 模型调 load_skill）。

触发纪律：description / triggers 必须写窄。「检查知识库」会让任何提到知识库
的对话都加载它；「新增或修改 KB 条目时使用」才准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# 正文进上下文的上限：一个 skill 是「打法」不是「手册」，超过这个长度说明
# 该拆成两个 skill，或者内容该进 KB
MAX_BODY_CHARS = 1200

# 隐式匹配的门槛：goal 里至少要原样出现一个 trigger。
# 不做模糊匹配的原因：误触发一次就是每轮多付 800 token，且可能把模型带偏。
DEFAULT_ROOT = Path("skills")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str = ""
    triggers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "triggers": list(self.triggers), "chars": len(self.body)}


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """极简 YAML 子集：只认 `key: value`，够用且没有依赖。

    不引 PyYAML 的原因：skill 的元数据只有 name/description/triggers 三个键，
    为一个三键头引一个解析器，出错面比收益大。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip()
    return meta, parts[2].lstrip("\n")


def _triggers_of(meta: dict[str, str], description: str) -> tuple[str, ...]:
    raw = meta.get("triggers") or ""
    items = [x.strip() for x in re.split(r"[,，;；|]", raw) if x.strip()]
    if items:
        return tuple(items)
    # 没写 triggers 时不猜——宁可只支持显式 load_skill，也不要满屏误触发
    return ()


@dataclass
class SkillBook:
    """一个目录 = 一个技能库。root 不存在时是空库（不报错，CLI 不该因此起不来）。"""

    root: Path = DEFAULT_ROOT
    skills: list[Skill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.reload()

    def reload(self) -> list[Skill]:
        found: list[Skill] = []
        if self.root.is_dir():
            for md in sorted(self.root.glob("*/SKILL.md")):
                try:
                    meta, body = _split_frontmatter(md.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    continue  # 单个坏文件不影响整库：跳过而不是让进程起不来
                name = meta.get("name") or md.parent.name
                desc = meta.get("description") or ""
                found.append(Skill(name=name, description=desc, body=body,
                                   path=str(md), triggers=_triggers_of(meta, desc)))
        self.skills = found
        return found

    def get(self, name: str) -> Optional[Skill]:
        for s in self.skills:
            if s.name == name:
                return s
        return None

    def index_text(self) -> str:
        """常驻上下文的那几行。返回空串时调用方不要插入段落（避免空标题）。"""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills)

    def match(self, goal: str) -> list[Skill]:
        """隐式命中：返回**唯一**得分最高的 skill；并列第一视为歧义，返回空。

        并列不选一个的原因：隐式触发一旦选错，模型会拿着错误打法跑完全程，
        比不加载更糟。显式 load_skill 随时可以兜底。
        """
        scored: list[tuple[int, Skill]] = []
        for s in self.skills:
            hits = sum(1 for t in s.triggers if t and t in goal)
            if hits:
                scored.append((hits, s))
        if not scored:
            return []
        scored.sort(key=lambda x: x[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return []
        return [scored[0][1]]

    def body_block(self, skills: list[Skill]) -> str:
        """命中后注入的正文段。每条截断：一个 skill 是打法，不是手册。"""
        blocks = []
        for s in skills:
            body = s.body.strip()
            if len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + "\n…（正文已截断，需要细节请调 load_skill 查看全文）"
            blocks.append(f"### {s.name}\n{body}")
        return "\n\n".join(blocks)


def register_skill_tool(registry: Any, book: SkillBook) -> None:
    """显式加载通道：模型自己决定「我要看哪个 skill 的正文」。

    做成普通只读工具而不是 loop 的特殊动作：加载 skill 本质就是「取一段
    已知文本」，它不需要新的执行语义，复用工具链才有统一的
    schema 校验 / 截断 / trace。
    """
    from app.core.tools import Tool

    async def load_skill(name: str) -> dict[str, Any]:
        skill = book.get(name)
        if skill is None:
            return {"error": f"没有名为 {name} 的 skill",
                    "available": [s.name for s in book.skills]}
        return {"name": skill.name, "description": skill.description, "body": skill.body}

    registry.register(Tool(
        name="load_skill",
        description="加载一个技能包（流程打法）的正文。先看上下文里的技能索引，"
                    "只有任务与某条描述匹配时才调用；一次只加载一个。",
        params={"name": {"type": "string", "description": "技能名（索引里的 name）",
                         "required": True}},
        handler=load_skill,
    ))


def build_skill_book(root: str | Path = DEFAULT_ROOT) -> SkillBook:
    return SkillBook(Path(root))
