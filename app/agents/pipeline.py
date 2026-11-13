"""处置编排状态机（T5）——从「分类器」到「Agent」的闭环

原计划用 LangGraph 骨架（intake → retrieve → decide → act → generate → verify），
最终改为自己写确定性状态机，理由与不用 LangChain 同源（面试重点）：

1. LangGraph 的图编排把「为什么走这条边」埋进框架抽象，本项目立身之本是每步
   可观测、可门禁、可评测——状态机 100 行，每一步的进出都有留痕（result["pipeline"]）；
2. 编排本身不需要智能：分几步、什么条件走哪条边是**业务规则**，让 LLM 参与编排
   等于给确定性流程引入不确定性；LLM 已经在 classify/仲裁节点里干活了；
3. 步数与预算上界是写死的不变量（max_steps=3 + Budget 贯穿），不存在「Agent 绕着
   门禁多绕几圈」的自由度——上限结构上保证，不靠 prompt 祈祷。

状态机（单步决策 + 确定性编排）：

    intake ──► classify ──► enrich ──► decide
    规整输入    复用分类引擎   只读信息收集   确定性处置映射

- classify：复用现有分类引擎（规则先行/LLM/降级链都在里面），编排层零改动接入
- enrich：needs_human_review 或灰区时，自动调**只读**工具拉同类历史样本，
  生成人工处置简报——「分类之后有动作」的第一步，全部只读：
  查错了最多信息冗余，写错了才是事故
- decide：确定性映射（不经过 LLM）——
  unknown → escalate（无处置建议，转人工兜底）
  低置信/证据拦截 → human_review（带简报升级人工，状态可持久化续跑）
  高置信 + 证据过门 → auto_dispose（自动处置）
"""

import logging
import time
from typing import Any, Optional

from app.core.budget import Budget
from app.core.tools import ToolRegistry
from app.observability.tracing import span

logger = logging.getLogger(__name__)

# 编排步数上界：状态机的边是确定的，这个值是结构性自检而非运行时变量——
# 正常路径恰好 3 步（classify/enrich/decide），超过说明代码改出了环，立即熔断
MAX_STEPS = 3


class HandlePipeline:
    """单请求处置编排：分类引擎 + 只读信息收集 + 确定性处置映射。

    classifier 复用 RootCauseClassifier（领域知识由 pack 注入）；
    registry 为该域的工具注册表（None = 该域无工具，enrich 如实跳过）。
    """

    def __init__(self, classifier, registry: Optional[ToolRegistry] = None,
                 max_steps: int = MAX_STEPS):
        self.classifier = classifier
        self.registry = registry
        self.max_steps = max_steps

    # ── 主入口 ──
    async def run(self, incident: dict[str, Any], budget: Optional[Budget] = None) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        with span("pipeline", stages=4) as s:
            result = await self._run_inner(incident, budget, steps)
            s["attrs"]["disposition"] = result.get("disposition")
            s["attrs"]["steps"] = len(steps)
        result["pipeline"] = steps
        return result

    async def _run_inner(self, incident, budget, steps) -> dict[str, Any]:
        # 1) intake：规整输入（防御 LLM/上游给来的非 dict 结构）
        result: dict[str, Any] = {"incident": incident if isinstance(incident, dict) else {}}
        self._step(steps, "intake", "ok", "输入规整完成", 0.0)

        # 2) classify：复用分类引擎，预算贯穿
        if self._over_limit(steps) or self._out_of_budget(budget, steps, result):
            return result
        t0 = time.monotonic()
        cls_result = await self.classifier.classify(incident, budget)
        self._step(steps, "classify", "ok",
                   f"class={cls_result.get('root_cause_class')} conf={cls_result.get('confidence')} "
                   f"source={cls_result.get('source')}", (time.monotonic() - t0) * 1000)
        result.update(cls_result)

        # 3) enrich：人工介入场景才做只读信息收集（高置信自动处置不需要简报）
        if self._over_limit(steps):
            return result
        if not result.get("needs_human_review") and result.get("root_cause_class") != "unknown":
            self._step(steps, "enrich", "skipped", "高置信自动处置，无需人工简报", 0.0)
        else:
            t0 = time.monotonic()
            brief = await self._collect_brief(result)
            status = "ok" if brief.get("samples") is not None else "skipped"
            self._step(steps, "enrich", status, brief.get("note", ""),
                       (time.monotonic() - t0) * 1000)
            result["human_brief"] = brief

        # 4) decide：确定性处置映射
        if self._over_limit(steps):
            return result
        self._decide(result, steps)
        return result

    # ── enrich：只读信息收集 ──
    async def _collect_brief(self, result: dict[str, Any]) -> dict[str, Any]:
        """给人工的处置简报：拉同类历史样本。只读工具，查错最多冗余、不会出错。"""
        if self.registry is None:
            return {"note": "该域未注册工具，跳过信息收集", "samples": None}
        tool = self.registry.get("query_incidents")
        if tool is None:
            return {"note": "注册表无 query_incidents，跳过信息收集", "samples": None}
        cls = result.get("root_cause_class")
        if not cls or cls == "unknown":
            return {"note": "类别为 unknown，无同类样本可拉", "samples": None}
        try:
            answer = await tool.handler(root_cause_class=cls, limit=3)
            return {"note": f"已附 {answer.get('matched', 0)} 条同类历史样本供人工参考",
                    "samples": answer.get("samples", [])}
        except Exception as e:  # 简报失败不阻塞主流程：分类结论已经可用
            logger.warning("enrich 信息收集失败",
                           extra={"event": "enrich_failed", "stage": "pipeline",
                                  "reason": str(e)[:150], "outcome": "degraded"})
            return {"note": f"信息收集失败（不阻塞主流程）: {str(e)[:80]}", "samples": None}

    # ── decide：确定性映射，无 LLM 参与 ──
    def _decide(self, result: dict[str, Any], steps: list[dict[str, Any]]) -> None:
        cls = result.get("root_cause_class")
        conf = result.get("confidence", 0.0)
        if cls == "unknown":
            disposition, reason = "escalate", "引擎无法归类（unknown），转人工兜底"
        elif result.get("needs_human_review"):
            why = result.get("evidence_warning") or f"置信度 {conf:.2f} 低于自动处置阈值"
            disposition, reason = "human_review", why
        else:
            disposition = "auto_dispose"
            reason = f"高置信（{conf:.2f}）且证据过门禁，按 suggested_action 自动处置"
        result["disposition"] = disposition
        result["disposition_reason"] = reason
        self._step(steps, "decide", "ok", f"{disposition}: {reason}", 0.0)

    # ── 人工接管后的续跑（T5-3）──
    def resume(self, state: dict[str, Any], approved_class: str,
               note: str = "", decided_by: str = "human") -> dict[str, Any]:
        """人工批准类别后从中断点续跑：不再重新分类（人已经裁决），直接重跑 decide。

        state 是 escalation 队列里持久化的中断快照（classify 结果 + incident），
        续跑结果带 override 留痕——谁批的、批了什么，可审计。
        """
        result = dict(state.get("classify_result") or {})
        result["incident"] = state.get("incident") or {}
        result["human_override"] = {
            "approved_class": approved_class,
            "note": note,
            "decided_by": decided_by,
        }
        result["root_cause_class"] = approved_class
        result["source"] = f"{result.get('source', '?')}+human_override"
        result["confidence"] = 1.0  # 人工裁决是新的置信上界
        result["needs_human_review"] = False
        steps: list[dict[str, Any]] = [{"step": 1, "stage": "human_override", "status": "ok",
                                        "detail": f"{approved_class}（{note or '无备注'}）", "ms": 0.0}]
        self._decide(result, steps)
        result["pipeline"] = steps
        return result

    # ── 上界守护 ──
    def _over_limit(self, steps: list[dict[str, Any]]) -> bool:
        # 只数编排动作（classify/enrich/decide）；intake 是规整不算步，abort 不重复计
        used = sum(1 for s in steps if s["stage"] not in ("intake", "abort", "human_override"))
        if used < self.max_steps:
            return False
        steps.append({"step": len(steps) + 1, "stage": "abort", "status": "failed",
                      "detail": f"步数超限（>{self.max_steps}），结构性熔断", "ms": 0.0})
        return True

    def _out_of_budget(self, budget: Optional[Budget], steps, result) -> bool:
        if budget is None or not budget.exhausted():
            return False
        steps.append({"step": len(steps) + 1, "stage": "abort", "status": "failed",
                      "detail": "预算耗尽，带当前最优退出", "ms": 0.0})
        result["pipeline_exit"] = {"reason": "budget_exhausted",
                                   "uncertainty": "未能完成完整编排，结论不完整"}
        return True

    @staticmethod
    def _step(steps: list, stage: str, status: str, detail: str, ms: float) -> None:
        steps.append({"step": len(steps) + 1, "stage": stage, "status": status,
                      "detail": detail[:160], "ms": round(ms, 1)})
