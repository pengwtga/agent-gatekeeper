"""领域包完整性：三个域都声明齐了、人设不空、prompt 占位符能拼上

2026-11-16 修的一个真缺陷：ssrm 的 AssistantSpec 没传 role_intro，而
rag_assistant 用 `spec.role_intro or "智能助手"` 兜底，于是 system prompt 第一句
退化成「你是智能助手。」——领域人设（只给建议、不臆造系统操作）根本没进 prompt，
只剩知识库条目的 resolution 文本在兜。这种"默认值悄悄吃掉配置"的洞，靠单测守住。
"""

import pytest

from app.domains.credit import pack as credit_pack
from app.domains.csa import pack as csa_pack
from app.domains.ssrm import pack as ssrm_pack
from app.prompts import load as load_prompt

PACKS = [ssrm_pack, csa_pack, credit_pack]


@pytest.mark.parametrize("pack", PACKS, ids=[p.name for p in PACKS])
def test_assistant_role_intro_declared(pack):
    """助手人设必须由领域包显式声明，不能靠引擎的「智能助手」兜底"""
    intro = pack.assistant.role_intro
    assert intro.strip(), f"{pack.name} 的 AssistantSpec.role_intro 为空"
    assert "智能助手" not in intro


@pytest.mark.parametrize("pack", PACKS, ids=[p.name for p in PACKS])
def test_classifier_role_intro_separate_from_assistant(pack):
    """两处 role_intro 语义不同：分类是系统拓扑知识，助手是人设边界，不该互相复用"""
    assert pack.role_intro != pack.assistant.role_intro


@pytest.mark.parametrize("pack", PACKS, ids=[p.name for p in PACKS])
def test_answer_prompt_renders_without_duplicated_sentence(pack):
    """v2 去掉了首句冗余：role_intro 本身已含「会来问你」，模板不该再拼一遍"""
    spec = pack.assistant
    text = load_prompt("rag_answer").render(
        role_intro=spec.role_intro or "智能助手", id_field=spec.id_field)
    first_line = text.splitlines()[0]
    assert first_line.startswith("你是")
    assert first_line.count("会来问你") <= 1, first_line
