"""F18 路由器单元测试：路由是混合架构的「第一道门」，判错方向比判错精度更致命。

测试替身 FakeRag 只复刻 route_request 消费的鸭子类型接口：
_keyword_search（bigram 覆盖率，与 RagAssistant 同口径）+ spec.class_field。
不 mock 私有链路，路由换了实现这组测试照样成立——测的是契约不是反射。
"""

from __future__ import annotations

import asyncio

from oncall.router import route_request
from tests.v2.stubs import ScriptedLLM


class FakeSpec:
    threshold = 0.4
    class_field = "root_cause_class"


class FakeRag:
    """与 RagAssistant._keyword_search 同口径的 bigram 覆盖率检索。"""

    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        self.spec = FakeSpec()
        self._doc_grams = None

    def _keyword_search(self, question: str, top_k: int = 5) -> list[dict]:
        from app.core.text import bigrams

        qg = bigrams(question)
        scored = []
        for t in self.docs:
            dg = bigrams(f"{t['title']}{t['symptom']}")
            score = len(qg & dg) / max(len(qg), 1)
            scored.append({**t, "score": round(score, 4)})
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]


def _docs() -> list[dict]:
    return [
        {"title": "审批通过但无任务", "symptom": "xFlow 工单已 FINISHED 但 SSRM 无对应注册任务",
         "root_cause_class": "xflow_callback_lost"},
        {"title": "同步超时", "symptom": "EA 供应商主数据同步超时，注册卡在 register 阶段",
         "root_cause_class": "ea_sync_timeout"},
        {"title": "同步超时无任务", "symptom": "EA 同步超时导致供应商注册无法创建任务",
         "root_cause_class": "ea_sync_timeout"},
        {"title": "重复提交", "symptom": "同一注册请求重复提交出现两条供应商记录",
         "root_cause_class": "duplicate_submit"},
    ]


def _split_docs() -> list[dict]:
    """三类同现象、票数 1/1/1 的语料：构造「覆盖强但方向分裂」。"""
    return [
        {"title": "回调丢失", "symptom": "供应商注册失败",
         "root_cause_class": "xflow_callback_lost"},
        {"title": "同步超时", "symptom": "供应商注册失败",
         "root_cause_class": "ea_sync_timeout"},
        {"title": "重复提交", "symptom": "供应商注册失败",
         "root_cause_class": "duplicate_submit"},
    ]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ── L0 风险预筛 ───────────────────────────────────────────────────────────
def test_risk_intent_routes_to_agent_even_when_kb_covers():
    """危险意图优先于一切能力信号：即使 KB 完全覆盖也不允许走 pipeline。"""
    rag = FakeRag(_docs())
    d = _run(route_request("请直接重启同步服务，EA 同步超时卡单了", rag))
    assert d.route == "agent"
    assert d.signals.get("risk") is True


# ── L1 覆盖探针 ───────────────────────────────────────────────────────────
def test_no_overlap_routes_to_refuse():
    rag = FakeRag(_docs())
    d = _run(route_request("停车场车牌识别道闸不抬杆，物业要求排查", rag))
    assert d.route == "refuse"


def test_strong_coverage_and_class_agreement_routes_to_pipeline():
    rag = FakeRag(_docs())
    d = _run(route_request("EA 供应商主数据同步超时，注册卡在 register 阶段无法创建", rag))
    assert d.route == "pipeline"
    assert d.signals["top_class"] == "ea_sync_timeout"


def test_coverage_but_split_classes_routes_to_agent():
    """top3 类别分裂 = 探索性任务，agent 安全失败，pipeline 不许接。"""
    rag = FakeRag(_split_docs())
    d = _run(route_request("供应商注册失败，请排查", rag))
    assert d.route == "agent"


def test_weak_band_with_consensus_routes_to_agent_not_refuse():
    """弱覆盖但类别有共识：值得探索（agent），不是库外（refuse）。"""
    rag = FakeRag(_docs())
    goal = "同步超时，供应商创建卡住"  # 与 doc2 部分重叠
    d = _run(route_request(goal, rag))
    assert d.route in ("agent", "pipeline")  # 弱覆盖共识 → 至少不 refuse


def test_weak_band_scattered_routes_to_refuse():
    rag = FakeRag(_docs())
    d = _run(route_request("系统有点不对劲，帮我看看日志配置问题", rag))
    assert d.route == "refuse"


# ── L2 LLM 仲裁（可选通道） ───────────────────────────────────────────────
def test_arbiter_high_confidence_upgrades_to_pipeline():
    rag = FakeRag(_split_docs())  # 强覆盖但三类同票 → 进仲裁
    llm = ScriptedLLM([{"root_cause_class": "xflow_callback_lost", "confidence": 0.9}])
    d = _run(route_request("供应商注册失败，请排查", rag, use_llm=True, llm=llm))
    assert d.route == "pipeline"
    assert d.signals["arbitration"]["verdict"] == "pipeline"


def test_arbiter_low_confidence_falls_back_to_agent():
    """仲裁低置信 → 落回 agent（安全默认）。仲裁失败不许抬高路由。"""
    rag = FakeRag(_split_docs())
    llm = ScriptedLLM([{"root_cause_class": "xflow_callback_lost", "confidence": 0.3}])
    d = _run(route_request("供应商注册失败，请排查", rag, use_llm=True, llm=llm))
    assert d.route == "agent"


def test_arbiter_exception_falls_back_to_agent():
    rag = FakeRag(_split_docs())

    class BoomLLM:
        async def chat_json(self, *a, **k):
            raise RuntimeError("provider down")

    d = _run(route_request("供应商注册失败，请排查", rag, use_llm=True, llm=BoomLLM()))
    assert d.route == "agent"
