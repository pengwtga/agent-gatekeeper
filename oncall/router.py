"""F18 路由器：请求进来的第一个决策点——这个任务是「确定性的」吗？

「确定性」不是问题的属性，是**信号的属性**。三个信号门全部确定，才允许走
固定 pipeline；任何一个不确定，降级到带门禁的 agent loop 或直接转人工：

  L0 风险预筛（规则，0 成本，永远最先）
     goal 命中危险意图模式（重启/清空/删除/关开关/强制下线…）→ agent
     ——交给 agent loop 的策略门禁走审批挂起。pipeline 没有审批语义，
     所以风险信号不允许路由到 pipeline。宁可多疑：误拦一次多花一次人工，
     漏拦一次是事故（与 S2 门禁同一代价哲学）。

  L1 覆盖探针（检索，0 次 LLM 调用）
     用知识库底层检索（向量+关键词 RRF，同 RagAssistant 通道、不调 LLM）
     探 goal，看「库内有没有可采信的覆盖、方向是否一致」：
     - 无采信命中（分数低于 spec.threshold）→ refuse：库外问题，转人工。
       依据是「检索无命中」这一客观事实，不是模型的主观判断；
     - 有命中且方向一致（top 命中多数同根因类）→ pipeline：已知模式，
       固定流程最稳、最便宜；
     - 有命中但方向分裂（top 类别胶着）→ agent：探索性任务，
       让带门禁的 loop 自由取证，错了也是安全失败。

  L2 LLM 仲裁（可选，默认关）
     L1 分裂时的仲裁通道：受限输出 + 置信度门禁，仲裁失败或低置信
     一律落回 agent（安全默认）。默认关的原因：路由器自己调用 LLM 会让
     「确定性路由」引入不确定性，评测不可复现；先只用客观信号。

设计红线：route() 只消费检索信号，不生成结论。路由器回答的是「这个问题
该由谁答」，不是「答案是什么」——把「要不要答」和「怎么答」分开，
是混合架构里唯一不可让渡的职责边界。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from oncall.rag_loop import strip_noise

# L0 危险意图模式：祈使句里的高危动词。误伤面可控——这些词出现在排障
# 诉求里，几乎总是「要求执行高危动作」，而不是「描述现象」。
_RISK_RE = re.compile(
    r"重启|清空|清库|清掉|删除|删掉|删了|全删|关掉|关闭|下线|踢掉|踢下线|回滚|强行|强制"
    r"|结束所有|断开所有|停掉"
    r"|drop\s+table|truncate|rm\s+-rf|force\s+push",
    re.IGNORECASE,
)

# L1 类别一致性门槛：top 类别至少 2 票、且占 top3 过六成才算「方向一致」。
AGREE_MIN_VOTES = 2
AGREE_MIN_RATIO = 0.6

# L1 分区阈值（keyword bigram 覆盖率口径，用 200 条数据集网格定标，见 F18 §4）：
#   top <  WEAK_BAND                      → refuse（几乎无词汇重叠 = 库外）
#   WEAK_BAND ≤ top < COVER_BAND          → 弱覆盖带：top3 类别仍有共识 → agent，
#                                           共识也散 → refuse
#   top ≥  COVER_BAND + 方向一致 + PIPE_MIN_TOP → pipeline
#   top ≥  COVER_BAND 但方向分裂          → agent
WEAK_BAND = 0.08
COVER_BAND = 0.15
PIPE_MIN_TOP = 0.16

PROBE_TOP_K = 5


@dataclass
class RouteDecision:
    route: str                      # "pipeline" | "agent" | "refuse"
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"route": self.route, "reason": self.reason, "signals": self.signals}


def _has_risk_intent(goal: str) -> bool:
    return bool(_RISK_RE.search(goal or ""))


async def _probe(rag: Any, goal: str) -> list[dict[str, Any]]:
    """L1 覆盖探针：固定走关键词通道（bigram 覆盖率，0-1 量纲稳定）。

    不用向量通道的原因：余弦分与覆盖率量纲不同，混用要维护两套阈值；
    探针必须便宜、确定、可复现——检索强弱本来就应由 rag_search 内部的
    agentic 循环负责（改写重试），路由层只回答「有没有得探」。
    """
    return rag._keyword_search(strip_noise(goal), top_k=PROBE_TOP_K)


async def route_request(goal: str, rag: Any, *, use_llm: bool = False,
                        llm: Any = None) -> RouteDecision:
    """路由决策入口。rag 是 RagAssistant（或兼容鸭子类型的测试替身）。"""
    # L0：风险预筛——先于一切能力信号
    if _has_risk_intent(goal):
        return RouteDecision("agent", "命中危险意图模式，交策略门禁走审批（pipeline 无审批语义）",
                             {"risk": True})

    # L1：覆盖探针
    hits = await _probe(rag, goal)
    top_score = hits[0]["score"] if hits else 0.0
    class_field = rag.spec.class_field
    top_classes = [str(h.get(class_field) or "") for h in hits[:3]]
    votes5 = Counter(top_classes).most_common(1)[0] if top_classes else ("", 0)
    # 候选簇数（去重、剔除空类）：F11 的并行取证判据直接用这个数——
    # 路由探针已经付过一次检索成本，不让它白跑，这是混合架构的协同点。
    candidates = [c for c in dict.fromkeys(top_classes) if c]
    spread = {"top_score": top_score, "class_spread": len(candidates),
              # strong_cover：这不是路由判据，是给下游（F11 并行取证）的护栏——
              # 弱覆盖带里的「多簇」往往是库外问题撞上了多个类的词面，
              # 拿它当「值得多角度探索」会让并行取证专门服务本该转人工的请求。
              "strong_cover": top_score >= COVER_BAND,
              "candidate_classes": candidates}

    if top_score < WEAK_BAND or not hits:
        return RouteDecision("refuse", "知识库几乎无词汇重叠（库外问题），转人工",
                             {"top_score": top_score, "admitted": 0, "class_spread": len(candidates),
                              "candidate_classes": candidates})

    if top_score < COVER_BAND:
        # 弱覆盖带：类别仍有共识 → 值得探索；共识也散 → 库外转人工
        if votes5[1] >= AGREE_MIN_VOTES:
            return RouteDecision("agent", "弱覆盖但类别有共识，交带门禁的 agent loop 取证",
                                 {**spread, "weak_band": True, "top_class": votes5[0]})
        return RouteDecision("refuse", "弱覆盖且类别共识散（库外问题），转人工",
                             {**spread, "weak_band": True})

    # 强覆盖带：方向一致才允许 pipeline，否则探索
    admitted = [h for h in hits if h["score"] >= COVER_BAND][:3]
    classes = [str(h.get(class_field) or "") for h in admitted]
    top_class, votes = Counter(classes).most_common(1)[0]
    agreement = votes / len(classes) if classes else 0.0
    admitted_classes = [c for c in dict.fromkeys(classes) if c]
    signals = {"top_score": top_score, "top_class": top_class,
               "votes": votes, "agreement": round(agreement, 2),
               "class_spread": len(admitted_classes),
               "candidate_classes": admitted_classes}

    if votes >= AGREE_MIN_VOTES and agreement >= AGREE_MIN_RATIO and top_score >= PIPE_MIN_TOP:
        return RouteDecision("pipeline", f"库内覆盖且方向一致（{top_class} x{votes}），固定流程处置",
                             signals)

    # L1 分裂：默认 agent（安全失败），可选 L2 仲裁
    if use_llm and llm is not None:
        arb = await _arbitrate(goal, top_class, classes, llm)
        signals["arbitration"] = arb
        if arb["verdict"] == "pipeline":
            return RouteDecision("pipeline", f"L2 仲裁归一类（{arb['cls']}，conf={arb['confidence']}）",
                                 signals)
    return RouteDecision("agent", "库内命中但类别方向分裂，交带门禁的 agent loop 取证", signals)


async def _arbitrate(goal: str, top_class: str, classes: list[str], llm: Any) -> dict[str, Any]:
    """L2 仲裁：一次受限输出的 LLM 调用。失败/低置信一律落回 agent。"""
    try:
        prompt = (
            "值班排障路由器。以下是用户现象和知识库 top 命中的根因类别：\n"
            f"现象：{goal}\n候选类别：{classes}\n"
            '只输出 JSON：{"root_cause_class": "候选之一或 unknown", "confidence": 0~1}')
        out = await llm.chat_json("你是排障路由仲裁器。", prompt)
        cls = str(out.get("root_cause_class") or "")
        conf = float(out.get("confidence") or 0.0)
        if cls in classes and conf >= 0.7:
            return {"verdict": "pipeline", "cls": cls, "confidence": conf}
        return {"verdict": "agent", "cls": cls or "unknown", "confidence": conf}
    except Exception as e:
        return {"verdict": "agent", "cls": "error", "confidence": 0.0,
                "note": str(e)[:80]}
