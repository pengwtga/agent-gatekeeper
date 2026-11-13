"""根因分类引擎（领域无关）— 知识由 DomainPack 注入，本模块只管机制

核心设计（面试重点）：
1. **引擎 / 领域分离**：分几类、每类定义、规则基线都是领域知识（见 app.domains），
   本模块只实现机制 —— 换领域 = 换 pack，本文件零改动
2. LLM 分类 + 规则基线双通道：LLM 不可用时自动降级，服务可用性不依赖外部 LLM
3. 输出强制 JSON：root_cause_class / confidence / evidence / suggested_action
4. 证据可回溯校验：evidence 里的锚点必须在输入原文里找得到，找不到就压置信度转人工
   —— 模型自报的 confidence 不可信，但"理由能不能对上输入"是白盒可验证的
5. 置信度门禁：低于阈值（0.85）建议人工确认 —— 沿用真实 SSRM "审批人工门禁" 的设计哲学
"""

import json as _json
import logging
from typing import Any, Optional

from app.agents.evidence import evidence_score
from app.config import settings
from app.core.budget import Budget
from app.core.llm_client import LLMClient
from app.domains import DomainPack

logger = logging.getLogger(__name__)


def _as_float(value: Any, default: float = 0.0) -> float:
    """模型可能返回 "0.9"、null 甚至 0.9 之外的值，统一收口到 [0, 1]"""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class RootCauseClassifier:
    """根因分类 Agent：LLM 主通道 + 领域规则基线降级通道

    pack 注入领域知识（类别体系 / few-shot / 规则基线），
    本类只负责通道编排、输出校验和两道门禁。
    """

    def __init__(self, llm: LLMClient, pack: DomainPack):
        self.llm = llm
        self.pack = pack

    async def classify(self, incident: dict[str, Any], budget: Optional[Budget] = None) -> dict[str, Any]:
        """分类入口。LLM 失败自动降级规则基线，结果标注 source。

        budget（T1-1）：LLM 通道在预算内尝试；预算耗尽或全链失败时降级规则基线，
        并标记 degraded=true + 原因 —— 调用方（值班同学/上层系统）必须能区分
        「这是 LLM 认真判断的」和「这是来不及认真判断的兜底结果」。
        """
        from app.observability.metrics import metrics

        # 规则先行通道（pack.rule_first）：规则给出明确结论直接采用，
        # 落 unknown（灰区）才升级 LLM 仲裁——信贷这类「策略即硬规则」领域，
        # 规则是权威，LLM 全量跑反而引入算术/规则应用误差（实测 66.7% vs 规则 96.7%）
        if self.pack.rule_first and self.pack.rule_baseline is not None:
            rule_result = self._rule_baseline(incident)
            if rule_result["root_cause_class"] != "unknown":
                rule_result["source"] = "rule_primary"
                metrics.record_degrade("none")
                metrics.record_class(rule_result["root_cause_class"])
                return rule_result
            # 灰区：升级 LLM 仲裁；LLM 结论可用时标注升级来源
            if self.llm.is_configured:
                try:
                    result = await self.llm.chat_json(
                        system_prompt=self._build_system(),
                        user_payload=_json.dumps(incident, ensure_ascii=False),
                        budget=budget,
                    )
                    result["root_cause_class"] = self._validate_class(result.get("root_cause_class"))
                    result["confidence"] = _as_float(result.get("confidence"))
                    result["evidence"] = str(result.get("evidence") or "")
                    result["suggested_action"] = str(result.get("suggested_action") or "")
                    self._apply_evidence_gate(result, incident)
                    result["source"] = "llm_arbiter"
                    result["llm_provider"] = result.pop("_provider", "unknown")
                    result["escalated_reason"] = rule_result["evidence"]
                    metrics.record_degrade("none")
                    metrics.record_confidence(result["confidence"])
                    metrics.record_class(result["root_cause_class"])
                    return result
                except Exception as e:
                    code = getattr(e, "code", "provider_error")
                    metrics.record_degrade(code)
                    rule_result["source"] = "rule_fallback"
                    rule_result["degraded"] = True
                    rule_result["degraded_reason"] = f"灰区升级 LLM 失败: {str(e)[:150]}"
                    rule_result["degraded_code"] = code
                    metrics.record_class(rule_result["root_cause_class"])
                    return rule_result
            # LLM 未配置：灰区如实转人工
            rule_result["source"] = "rule_fallback"
            metrics.record_degrade("not_configured")
            metrics.record_class(rule_result["root_cause_class"])
            return rule_result

        if self.llm.is_configured:
            try:
                result = await self.llm.chat_json(
                    system_prompt=self._build_system(),
                    user_payload=_json.dumps(incident, ensure_ascii=False),
                    budget=budget,
                )
                result["root_cause_class"] = self._validate_class(result.get("root_cause_class"))
                result["confidence"] = _as_float(result.get("confidence"))
                result["evidence"] = str(result.get("evidence") or "")
                result["suggested_action"] = str(result.get("suggested_action") or "")
                self._apply_evidence_gate(result, incident)
                result["source"] = "llm"
                result["llm_provider"] = result.pop("_provider", "unknown")
                metrics.record_degrade("none")
                # 只统计 LLM 通道的置信度分布：规则通道的置信度是代码里写死的常数
                # （0.8/0.85/0.9），混进来会让直方图变成两根柱子，看不出模型行为
                metrics.record_confidence(result["confidence"])
                metrics.record_class(result["root_cause_class"])
                return result
            except Exception as e:
                code = getattr(e, "code", "provider_error")
                metrics.record_degrade(code)
                logger.warning(
                    "LLM 降级到规则基线",
                    extra={
                        "event": "classify_degrade", "stage": "classify",
                        "reason": f"{code}: {str(e)[:160]}", "outcome": "degraded",
                    },
                )
                result = self._rule_baseline(incident)
                result["source"] = "rule_fallback"
                result["degraded"] = True
                result["degraded_reason"] = str(e)[:200]
                result["degraded_code"] = code
                metrics.record_class(result["root_cause_class"])
                return result

        # LLM 从未配置：这是部署形态而非故障，不算 degraded
        metrics.record_degrade("not_configured")
        result = self._rule_baseline(incident)
        result["source"] = "rule_fallback"
        metrics.record_class(result["root_cause_class"])
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
                "证据无法回溯",
                extra={
                    "event": "evidence_gate_blocked", "stage": "classify",
                    "score": score, "root_cause_class": result["root_cause_class"],
                    "reason": result["evidence"][:80], "outcome": "degraded",
                },
            )

        result["needs_human_review"] = result["confidence"] < settings.min_confidence_for_auto_action

    def _build_system(self) -> str:
        """system prompt + few-shot 组装（领域知识来自 pack）"""
        classes_block = "\n".join(f"- {k}: {v}" for k, v in self.pack.classes.items())
        system_prompt = f"""你是{self.pack.role_intro}。

{self.pack.name.upper()} 的 {len(self.pack.classes)} 类根因定义及标准处置：
{classes_block}

任务：根据输入的事件上下文（JSON），判断根因类别并给出处置建议。
规则：
1. root_cause_class 必须是上述 {len(self.pack.classes)} 个枚举值之一
2. confidence 为 0~1 的小数，表示你对该判断的确信程度；证据不足时如实给低分
3. evidence 引用输入中的具体字段和值作为依据（1~2 句话）
4. suggested_action 用一句话给出处置建议，可参考各类标准处置
5. 只输出 JSON：{{"root_cause_class": "...", "confidence": 0.0, "evidence": "...", "suggested_action": "..."}}"""

        lines = [system_prompt]
        if self.pack.few_shot:
            lines.append("\n示例：")
            for msg in self.pack.few_shot:
                tag = "输入" if msg["role"] == "user" else "输出"
                lines.append(f"[{tag}] {msg['content']}")
        return "\n".join(lines)

    def _validate_class(self, value: Any) -> str:
        if value in self.pack.classes:
            return value
        logger.warning(
            "非法类别输出，降级为 unknown",
            extra={"event": "invalid_class", "stage": "classify",
                   "reason": repr(value)[:80], "outcome": "degraded"},
        )
        return "unknown"

    def _rule_baseline(self, incident: dict[str, Any]) -> dict[str, Any]:
        """规则基线：实现在领域包里（关键字/阈值规则是领域知识）"""
        if self.pack.rule_baseline is not None:
            return self.pack.rule_baseline(incident)
        # 领域没提供规则基线：与 LLM 输出同构的 unknown 兜底
        return {
            "root_cause_class": "unknown",
            "confidence": 0.2,
            "evidence": "该领域未配置规则基线",
            "suggested_action": "转人工排查",
            "needs_human_review": True,
        }
