"""F10：Skill 运行时加载。

钉死四件事：
1. 加一个 skill = 加目录 + md，不改代码就能被索引/命中（可插拔是唯一卖点）；
2. 未命中时只付一行索引的钱（渐进披露的结构性优势）；
3. 触发必须窄：问「知识库怎么备份」不许加载 kb-entry（误触发 = 每轮多付 800 token）；
4. 加载走工具链（只读 + schema 校验 + 截断），不是 loop 的特例分支。
"""

import asyncio
from pathlib import Path

from oncall.context import build_messages
from oncall.skills_loader import MAX_BODY_CHARS, SkillBook, register_skill_tool
from oncall.tools import call_tool
from tests.v2.stubs import build_registry

REPO_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def _write_skill(root: Path, name: str, desc: str, triggers: str, body: str = "# 正文\n步骤") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\ntriggers: {triggers}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_repo_skills_are_indexed_one_line_each():
    book = SkillBook(REPO_SKILLS)
    names = {s.name for s in book.skills}
    assert {"incident-triage", "postmortem", "kb-entry"} <= names

    index = book.index_text()
    assert len(index.splitlines()) == len(book.skills)
    # 每个 skill 常驻只花一行：描述长度要可控（约 20 token 量级）
    assert all(len(line) < 160 for line in index.splitlines())


def test_new_skill_is_picked_up_without_code_change(tmp_path):
    """验收 1：只加目录 + md，不改代码即可命中。"""
    _write_skill(tmp_path, "rollback-runbook", "回滚一次发布的标准动作",
                 "回滚, 版本回退", body="# 回滚\n1. 冻结发布 2. 回退上一个 tag 3. 校验")
    book = SkillBook(tmp_path)

    assert [s.name for s in book.skills] == ["rollback-runbook"]
    assert "rollback-runbook" in book.index_text()
    matched = book.match("刚发的版本有问题，需要回滚")
    assert [s.name for s in matched] == ["rollback-runbook"]
    assert "冻结发布" in book.body_block(matched)


def test_implicit_match_is_narrow():
    """验收 3：description/triggers 写窄，问备份这种相邻话题不许触发入库技能。"""
    book = SkillBook(REPO_SKILLS)

    assert book.match("知识库怎么备份？") == []
    assert book.match("这次事故写个复盘")[0].name == "postmortem"
    assert book.match("帮我新增知识库条目：xx 类问题的处置")[0].name == "kb-entry"


def test_ambiguous_match_loads_nothing(tmp_path):
    """并列第一视为歧义：选错打法比不加载更糟，交给显式 load_skill。"""
    _write_skill(tmp_path, "a", "A 流程", "重启")
    _write_skill(tmp_path, "b", "B 流程", "重启")
    book = SkillBook(tmp_path)

    assert book.match("服务要重启一下") == []


def test_body_block_truncates_long_skill(tmp_path):
    _write_skill(tmp_path, "long", "超长技能", "超长", body="x" * (MAX_BODY_CHARS + 500))
    book = SkillBook(tmp_path)

    block = book.body_block(book.skills)
    assert len(block) < MAX_BODY_CHARS + 200
    assert "已截断" in block


def test_load_skill_tool_reads_body_and_unknown_name_reports_alternatives():
    book = SkillBook(REPO_SKILLS)
    reg = build_registry()
    register_skill_tool(reg, book)

    ok = asyncio.run(call_tool(reg, "load_skill", {"name": "incident-triage"}))
    assert ok.ok and "值班排障顺序" in str(ok.value)

    bad = asyncio.run(call_tool(reg, "load_skill", {"name": "nope"}))
    assert bad.ok and "没有名为 nope" in str(bad.value)
    assert "incident-triage" in str(bad.value)


def test_skill_index_and_body_go_into_prompt():
    book = SkillBook(REPO_SKILLS)
    matched = book.match("注册卡单，帮我看下为什么")
    system, payload = build_messages(
        "注册卡单，帮我看下为什么", [], [], [],
        skill_index=book.index_text(), skill_block=book.body_block(matched))

    assert "## 可用技能" in payload
    assert "## 已加载技能" in payload
    assert "值班排障顺序" in payload          # 命中正文进了上下文
    assert "load_skill" in system             # 规则里告诉模型显式加载通道
