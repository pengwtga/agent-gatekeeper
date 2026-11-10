"""根因分类 Agent — SSRM 供应商准入/变更卡单与同步失败

核心设计（面试重点）：
1. 分类体系来自真实 oncall 归因：5 类根因覆盖 SSRM 注册/变更链路 90%+ 的卡单场景
2. LLM 分类 + 规则基线双通道：LLM 不可用时自动降级，服务可用性不依赖外部 LLM
3. 输出强制 JSON：root_cause_class / confidence / evidence / suggested_action
4. 证据可回溯校验：evidence 里的锚点必须在输入原文里找得到，找不到就压置信度转人工
   —— 模型自报的 confidence 不可信，但"理由能不能对上输入"是白盒可验证的
5. 置信度门禁：低于阈值（0.85）建议人工确认 —— 沿用真实 SSRM "审批人工门禁" 的设计哲学
"""

import logging
from typing import Any, Optional

from app.config import settings
from app.evidence import evidence_score
from app.llm_client import LLMClient, LLMNotConfiguredError

logger = logging.getLogger(__name__)

# ── 分类体系：5 类根因（每类绑定标准处置动作，来自真实 oncall 处置经验）──
ROOT_CAUSE_CLASSES: dict[str, str] = {
    "xflow_callback_lost": "审批回调丢失：xFlow 审批已通过，但回调未到达 SSRM（回调链路故障/服务重启窗口）。处置：按工单号主动拉取 xFlow 状态补齐，无需打扰用户。",
    "ea_sync_timeout": "同步 EA 超时/失败：向公司级供应商平台 EA 写数据超时（网络抖动/EA 发布中/接口限流）。处置：异步任务退避重试，超过阈值转人工；绝不阻塞注册主流程。",
    "duplicate_submit": "重复提交/幂等冲突：前端重复点击或 MQ 重复消费导致同一单多次到达。处置：幂等键去重直接返回成功，无需人工介入。",
    "data_inconsistency": "数据不一致：多下游同步部分成功（如 EA 成功但 Dora 缺联系人）。处置：由对账任务发现并补推，确认对账是否按时运行。",
    "frontend_param_error": "前端/参数错误：请求字段缺失、格式非法或前端调用方式错误。处置：拒绝请求并反馈具体字段问题，推动前端修复调用。",
}

SYSTEM_PROMPT = f"""你是 SSRM（供应商管理系统）运维根因分析专家。SSRM 的供应商注册/变更流程横跨多个系统：
- xFlow：部门通用审批流组件，审批完成后回调 SSRM 创建任务
- EA：公司级供应商主数据平台，SSRM 的供应商增删改查都同步给它
- Dora：对外供应商门户，供应商在 Dora 注册并提交材料

卡单/失败的 5 类根因定义及标准处置：
{chr(10).join(f'- {k}: {v}' for k, v in ROOT_CAUSE_CLASSES.items())}

任务：根据输入的事件上下文（JSON），判断根因类别并给出处置建议。
规则：
1. root_cause_class 必须是上述 5 个枚举值之一
2. confidence 为 0~1 的小数，表示你对该判断的确信程度；证据不足时如实给低分
3. evidence 引用输入中的具体字段和值作为依据（1~2 句话）
4. suggested_action 用一句话给出处置建议，可参考各类标准处置
5. 只输出 JSON：{{"root_cause_class": "...", "confidence": 0.0, "evidence": "...", "suggested_action": "..."}}"""

# few-shot 示例（固定 2 个，覆盖易混淆对）
FEW_SHOT = [
    {
        "role": "user",
        "content": '{"stage":"register","source_system":"xflow","error_code":null,"error_message":"审批单 FT20261023001 状态为 APPROVED，但 SSRM 无对应注册任务","wait_minutes":42,"retry_count":0,"downstream":"ssrm","http_status":null}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"xflow_callback_lost","confidence":0.95,"evidence":"审批单已 APPROVED 且 SSRM 无任务，说明回调未到达而非审批未完成","suggested_action":"按工单号主动拉取 xFlow 状态补齐注册任务，并检查回调链路监控"}',
    },
    {
        "role": "user",
        "content": '{"stage":"register","source_system":"ssrm","error_code":"EA_TIMEOUT","error_message":"调用 EA createSupplier 接口 5s 超时","wait_minutes":2,"retry_count":3,"downstream":"ea","http_status":504}',
    },
    {
        "role": "assistant",
        "content": '{"root_cause_class":"ea_sync_timeout","confidence":0.97,"evidence":"error_code=EA_TIMEOUT 且 http_status=504，重试 3 次均失败，指向 EA 侧超时","suggested_action":"异步任务退避重试，若 EA 处于发布窗口则等待恢复后由对账任务补推"}',
    },
]


def _as_float(value: Any, default: float = 0.0) -> float:
    """模型可能返回 "0.9"、null 甚至 0.9 之外的值，统一收口到 [0, 1]"""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class RootCauseClassifier:
    """根因分类 Agent：LLM 主通道 + 规则基线降级通道"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def classify(self, incident: dict[str, Any]) -> dict[str, Any]:
        """分类入口。LLM 失败自动降级规则基线，结果标注 source。"""
        import json as _json

        if self.llm.is_configured:
            try:
                result = await self.llm.chat_json(
                    system_prompt=self._build_system(),
                    user_payload=_json.dumps(incident, ensure_ascii=False),
                )
                result["root_cause_class"] = self._validate_class(result.get("root_cause_class"))
                result["confidence"] = _as_float(result.get("confidence"))
                result["evidence"] = str(result.get("evidence") or "")
                result["suggested_action"] = str(result.get("suggested_action") or "")
                self._apply_evidence_gate(result, incident)
                result["source"] = "llm"
                result["llm_provider"] = result.pop("_provider", "unknown")
                return result
            except Exception as e:
                logger.warning(f"[RootCauseClassifier] LLM 降级到规则基线: {e}")

        result = self._rule_baseline(incident)
        result["source"] = "rule_fallback"
        return result

    def _apply_evidence_gate(self, result: dict[str, Any], incident: dict[str, Any]) -> None:
        """证据门禁：理由对不回输入原文 → 压低置信度、强制人工。

        只作用于 LLM 通道：规则通道的 evidence 是代码用结构化字段拼出来的，
        不存在"编造"这一风险，套用同一个匹配器只会把正确的规则判断误伤成低置信。
        """
        score = evidence_score(result["evidence"], incident)
        result["evidence_score"] = score

        if score < settings.min_evidence_score:
            result["confidence"] = min(result["confidence"], settings.low_evidence_confidence_cap)
            result["evidence_warning"] = "判断依据未能回溯到输入原文，已压置信度并建议人工复核"
            logger.warning(
                "[RootCauseClassifier] 证据无法回溯 score=%.3f class=%s evidence=%r",
                score, result["root_cause_class"], result["evidence"][:80],
            )

        result["needs_human_review"] = result["confidence"] < settings.min_confidence_for_auto_action

    def _build_system(self) -> str:
        """system prompt + few-shot 组装"""
        lines = [SYSTEM_PROMPT, "", "示例："]
        for msg in FEW_SHOT:
            tag = "输入" if msg["role"] == "user" else "输出"
            lines.append(f"[{tag}] {msg['content']}")
        return "\n".join(lines)

    def _validate_class(self, value: Any) -> str:
        if value in ROOT_CAUSE_CLASSES:
            return value
        logger.warning(f"[RootCauseClassifier] 非法类别输出: {value!r}，降级为规则判断")
        return "unknown"

    # ── 规则基线：基于结构化特征的关键字/阈值规则（也是评测的 baseline）──
    def _rule_baseline(self, incident: dict[str, Any]) -> dict[str, Any]:
        msg = str(incident.get("error_message", "")).lower()
        code = str(incident.get("error_code") or "").upper()
        downstream = str(incident.get("downstream") or "").lower()
        http_status = incident.get("http_status")
        retry_count = int(incident.get("retry_count") or 0)

        if code in ("EA_TIMEOUT", "EA_5XX") or http_status in (504, 503) or (
            downstream == "ea" and "timeout" in msg or "timed out" in msg
        ):
            return self._make("ea_sync_timeout", 0.8,
                              f"error_code={code}, http_status={http_status}, downstream=ea")
        if "approved" in msg and ("无对应" in msg or "no task" in msg or "回调" in msg) or (
            incident.get("source_system") == "xflow"
            and incident.get("wait_minutes", 0) >= 30
        ) or code == "CALLBACK_MISSING":
            return self._make("xflow_callback_lost", 0.85,
                              "审批已通过但 SSRM 侧无任务，回调链路异常")
        if "duplicate" in msg or "幂等" in msg or code in ("DUPLICATE_KEY", "IDEMPOTENT_CONFLICT"):
            return self._make("duplicate_submit", 0.9, "命中幂等冲突特征")
        if code in ("PARAM_MISSING", "PARAM_INVALID") or "missing field" in msg or (
            incident.get("downstream") == "frontend"
        ):
            return self._make("frontend_param_error", 0.85, "参数/前端调用错误特征")
        if "inconsisten" in msg or "缺失" in msg or "missing" in msg and downstream in ("dora", "ea"):
            return self._make("data_inconsistency", 0.7, "部分成功导致的数据缺口特征")
        if downstream == "ea":
            return self._make("ea_sync_timeout", 0.5, "下游为 EA，默认按同步超时处理")
        # 兜底：证据不足，如实返回 unknown（宁可转人工也不瞎猜）
        return {
            "root_cause_class": "unknown",
            "confidence": 0.2,
            "evidence": "规则无法匹配任何已知模式",
            "suggested_action": "转人工排查，并将该 case 归因后沉淀为新规则",
            "needs_human_review": True,
        }

    @staticmethod
    def _make(cls: str, conf: float, evidence: str) -> dict[str, Any]:
        return {
            "root_cause_class": cls,
            "confidence": conf,
            "evidence": evidence,
            "suggested_action": (
                ROOT_CAUSE_CLASSES[cls].split("处置：", 1)[-1] if cls in ROOT_CAUSE_CLASSES else "人工排查"
            ),
            "needs_human_review": conf < settings.min_confidence_for_auto_action,
        }
