"""知识库入库校验（T5-05）：枚举/必填/唯一/分组一致，以及「组内措辞不许各写各的」

这组用例同时是回归护栏：三份真实 KB 必须全绿。校验器上线当天它抓到两类真问题——
ssrm 40 行 standard_action 同组不一致（手写 30 条母本时同一个类写了 2~3 种措辞），
credit 120 行 category 根本不在任何枚举里（助手类目与分类词表是两套，却没人声明）。
这两件事以前都只靠「我记得检查过」。

主体用内存入口 validate_rows（纯函数、不碰磁盘）；只有「文件级行为」（坏行号、
启动期抛错）才落盘，落在 tests/_tmp_kb/ 下并自清理。
"""

import json
from pathlib import Path

import pytest

from app.domains import AssistantSpec, ConsistencyRule
from app.domains.credit import pack as credit_pack
from app.domains.credit.taxonomy import COLLECTION_STAGES, CREDIT_CLASSES
from app.domains.csa import pack as csa_pack
from app.domains.ssrm import pack as ssrm_pack
from app.domains.ssrm.taxonomy import ROOT_CAUSE_CLASSES, STANDARD_ACTIONS
from app.kb.validate import (
    E_CLASS_UNKNOWN,
    E_DUP_ID,
    E_EMPTY_FIELD,
    E_INCONSISTENT,
    E_MISSING_FIELD,
    E_NOT_RECALLABLE,
    E_PARSE,
    KBValidationError,
    load_kb,
    validate_rows,
    validate_spec,
)

PACKS = [ssrm_pack, csa_pack, credit_pack]
CLASSES = {"a": "类 A", "b": "类 B"}


def _row(id_, cls="a", **over):
    row = {"id": id_, "cls": cls, "title": "一个足够长的标题",
           "symptom": "一段足够长的现象描述，用来占位",
           "resolution": "一段足够长的处置过程描述，用来占位"}
    row.update(over)
    return row


def _check(rows, **kw):
    opts = dict(id_field="id", class_field="cls", classes=CLASSES,
                required_fields=("title", "symptom", "resolution"))
    opts.update(kw)
    return validate_rows(rows, **opts)


def _codes(issues):
    return [i.code for i in issues]


_DATA = Path(__file__).resolve().parents[1] / "data"  # 已存在的目录：只写文件、不建目录


@pytest.fixture(autouse=True)
def _clean_tmp_files():
    yield
    for p in _DATA.glob("_tmp_kb_*.jsonl"):
        p.unlink()


def _write(rows, name):
    p = _DATA / f"_tmp_kb_{name}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")
    return p


def _spec(path, **kw):
    base = dict(kb_file=path, id_field="id", class_field="cls", classes=CLASSES,
                required_fields=("title", "symptom", "resolution"))
    base.update(kw)
    return AssistantSpec(**base)


# ── 回归护栏：三份真实知识库必须干净 ──
@pytest.mark.parametrize("pack", PACKS, ids=[p.name for p in PACKS])
def test_real_kb_is_valid(pack):
    issues = validate_spec(pack.assistant)
    assert [i for i in issues if i.level == "error"] == []


@pytest.mark.parametrize("pack", PACKS, ids=[p.name for p in PACKS])
def test_real_kb_cluster_self_recall(pack):
    """每条都要能被同簇条目召回：写得像人话，线上才召得回同类案例（O(n²)，仅 CI）"""
    issues = validate_spec(pack.assistant, recall=True)
    assert [i for i in issues if i.code == E_NOT_RECALLABLE] == []


# ── 枚举：本项目最想拦的一类错（ea_timeout vs ea_sync_timeout） ──
def test_unknown_class_is_error_with_hint():
    errs = [i for i in _check([_row("1", cls="aa")]) if i.code == E_CLASS_UNKNOWN]
    assert len(errs) == 1
    assert errs[0].line == 1
    assert "'a'" in errs[0].message  # difflib 给出最接近的合法值


def test_class_check_skipped_without_enum():
    """没给枚举就只剩结构层：这本身要报 warning，否则等于悄悄少一道关"""
    issues = _check([_row("1", cls="anything")], classes=None)
    assert E_CLASS_UNKNOWN not in _codes(issues)
    assert "W_NO_CLASS_ENUM" in _codes(issues)


# ── 必填 / 空值 / 重复 ID ──
def test_missing_and_empty_fields():
    rows = [{"id": "1", "cls": "a", "title": "足够长的标题", "symptom": "足够长的现象描述"},
            _row("2", symptom="   ")]
    codes = _codes(_check(rows))
    assert E_MISSING_FIELD in codes and E_EMPTY_FIELD in codes


def test_duplicate_id_reports_second_line():
    errs = [i for i in _check([_row("X1"), _row("X2"), _row("X1")]) if i.code == E_DUP_ID]
    assert len(errs) == 1 and errs[0].line == 3


def test_singleton_class_warns_not_errors():
    codes = _codes(_check([_row("1", cls="a"), _row("2", cls="a"), _row("3", cls="b")]))
    assert "W_SINGLETON_CLASS" in codes


# ── 分组一致：同组各写各的措辞 = 有人写错了 ──
def test_group_consistency_flags_deviation():
    rows = [_row("1", action="补齐"), _row("2", action="补齐"), _row("3", action="同步")]
    errs = [i for i in _check(rows, consistency=(ConsistencyRule("cls", ("action",)),))
            if i.code == E_INCONSISTENT]
    assert len(errs) == 1
    assert errs[0].line == 3
    assert "补齐" in errs[0].message and "第 1 行" in errs[0].message


# ── 召回体检：措辞完全另起炉灶的条目要被拦下 ──
def test_recall_flags_off_vocabulary_entry():
    """簇内用词与同簇其它条目完全不重合时才会被判失败（Top-K 里得真有一条同簇的）"""
    rows = [
        _row("1", cls="a", title="审批通过但无注册任务", symptom="xflow 审批已通过但注册任务未创建，回调丢失"),
        _row("2", cls="a", title="审批已完成缺任务", symptom="xflow 审批通过后注册任务没生成，回调没到"),
        _row("3", cls="a", title="回调未达导致缺单", symptom="xflow 回调没到达 ssrm，注册任务缺失"),
        _row("4", cls="a", title="回调丢失补单", symptom="xflow 审批通过，回调丢失，需补建注册任务"),
        _row("5", cls="b", title="票据流水异常", symptom="账务侧票据流水中转结算批次异常"),
        _row("6", cls="b", title="开户行信息缺失", symptom="供应商银行账户开户行字段为空"),
    ]
    errs = [i for i in _check(rows, recall=True) if i.code == E_NOT_RECALLABLE]
    assert [i.line for i in errs] == [5, 6]  # b 的两条互相也不像，且都排不进同簇


# ── 文件级行为：行号对得上、启动期该炸就炸 ──
def test_bad_json_reports_its_own_line():
    """空行不该把行号顶偏，否则修复时找不到那一行"""
    p = _write([_row("1"), "", "{不是 JSON", _row("2")], "parse")
    errs = [i for i in validate_spec(_spec(p)) if i.code == E_PARSE]
    assert len(errs) == 1 and errs[0].line == 3


def test_load_kb_raises_on_error():
    p = _write([_row("1", cls="zzz")], "badclass")
    with pytest.raises(KBValidationError) as e:
        load_kb(_spec(p))
    assert "E_CLASS_UNKNOWN" in str(e.value)


def test_load_kb_allows_warnings_only():
    """标题过短只是提醒，不该把服务拦死在启动期"""
    p = _write([_row("1"), _row("2"), _row("3", title="短")], "warn")
    assert len(load_kb(_spec(p))) == 3


# ── 领域装配：一套枚举注进去，两套词表各自声明 ──
def test_pack_injects_classes_into_assistant():
    assert ssrm_pack.assistant.classes.keys() == ROOT_CAUSE_CLASSES.keys()
    assert ssrm_pack.assistant.classes is not ROOT_CAUSE_CLASSES  # 副本，领域后续改动不会串
    assert csa_pack.assistant.classes is not None


def test_credit_declares_its_own_vocabulary():
    """credit 的助手类目是催收场景，不是进件风险——显式声明，别让引擎猜"""
    assert credit_pack.assistant.classes.keys() == COLLECTION_STAGES.keys()
    assert set(COLLECTION_STAGES).isdisjoint(CREDIT_CLASSES)


def test_standard_actions_cover_every_class():
    """动作标签与分类体系的键必须一一对应：加类时忘了补动作，KB 生成会 KeyError"""
    assert STANDARD_ACTIONS.keys() == ROOT_CAUSE_CLASSES.keys()


def test_keyword_search_uses_shared_bigrams():
    """检索与体检共用 app/core/text.bigrams —— 拆公共件时漏 import，只有跑到检索才炸"""
    from app.agents.rag_assistant import RagAssistant
    from app.core.llm_client import llm_client

    rag = RagAssistant(llm_client, ssrm_pack.assistant)
    rag.docs = [{"ticket_id": "A", "title": "审批回调丢失",
                 "symptom": "审批已通过但 SSRM 无注册任务",
                 "root_cause_class": "xflow_callback_lost", "resolution": "补齐"}]
    hits = rag._keyword_search("审批回调丢失", 1)
    assert hits and hits[0]["ticket_id"] == "A" and hits[0]["score"] > 0.5
