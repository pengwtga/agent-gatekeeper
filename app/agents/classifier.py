"""根因分类引擎（领域无关）— 知识由 DomainPack 注入，本模块只管机制

核心设计（面试重点）：
1. **引擎 / 领域分离**：分几类、每类定义、规则基线都是领域知识（见 app.domains），
   本模块只实现机制 —— 换领域 = 换 pack，本文件零改动
2. LLM 分类 + 规则基线双通道：LLM 不可用时自动降级，服务可用性不依赖外部 LLM
3. 输出强制 JSON：root_cause_class / confidence / evidence / suggested_action
4. 证据可回溯校验：evidence_refs 里的「字段 + 值」必须在输入里找得到，找不到就压置信度转人工
   —— 模型自报的 confidence 不可信，但「理由能不能对上输入」是白盒可验证的
5. 置信度门禁：低于阈值（0.85）建议人工确认 —— 沿用真实 SSRM "审批人工门禁" 的设计哲学
"""

import json as _json
import logging
from typing import Any, Optional

from app.agents.evidence import check_refs, evidence_score, refs_score
from app.config import settings
from app.core.budget import Budget
from app.core.llm_client import LLMClient
from app.domains import DomainPack
from app.prompts import load as load_prompt

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
        # prompt 外置（T3-1）：构造即加载，配置错了在启动阶段就炸，不带病跑
        self._prompt = load_prompt("root_cause")

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
                    result["prompt_version"] = self._prompt.label
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
                # T3-4：结果自带 prompt 版本，线上问题可反推到「当时用的是哪版 prompt」
                result["prompt_version"] = self._prompt.label
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

        两条判据，优先级明确（T3-5）：
        1. `evidence_refs`（结构化引用）：字段存在 + 值相等。与文本长短、措辞无关
        2. `evidence_score`（字面匹配，T0-03）：仅在 refs 缺失时使用，保证老模型不掉功能
        """
        detail = check_refs(result.get("evidence_refs"), incident)
        score = refs_score(detail)

        if score is not None:
            result["evidence_refs"] = detail  # 换成带 ok/actual 的明细，前端直接渲染引用条
            result["evidence_mode"] = "refs"
            result["evidence_score"] = score
            result["evidence_refs_count"] = len(detail)
            # 两把尺子同时记：线上可以直接对比两个判据的分歧率，作为「该不该彻底删掉字面匹配」的依据
            result["evidence_literal_score"] = evidence_score(result["evidence"], incident)

            valid = sum(1 for d in detail if d["ok"])
            if len(detail) < settings.min_evidence_refs:
                reason = f"仅 {len(detail)} 条引用（下限 {settings.min_evidence_refs}）"
            elif score < settings.min_evidence_refs_score:
                reason = f"有效引用 {valid}/{len(detail)}"
            else:
                reason = ""
            if reason:
                self._block_evidence(result, f"判断依据的结构化引用不足（{reason}），已压置信度并建议人工复核")
        else:
            # 回退：模型没按 schema 输出 refs（老模型 / 只吐了自由文本）
            result["evidence_mode"] = "literal"
            result["evidence_score"] = evidence_score(result["evidence"], incident)
            if result["evidence_score"] < settings.min_evidence_score:
                self._block_evidence(
                    result, "判断依据未能回溯到输入原文，已压置信度并建议人工复核")

        result["needs_human_review"] = result["confidence"] < settings.min_confidence_for_auto_action

    def _block_evidence(self, result: dict[str, Any], warning: str) -> None:
        """压置信度（不否决结论）：结论可能是对的，只是理由没引对，转人工比丢结果有用"""
        result["confidence"] = min(result["confidence"], settings.low_evidence_confidence_cap)
        result["evidence_warning"] = warning
        logger.warning(
            "证据无法回溯",
            extra={
                "event": "evidence_gate_blocked", "stage": "classify",
                "score": result.get("evidence_score"), "mode": result.get("evidence_mode"),
                "root_cause_class": result["root_cause_class"],
                "reason": warning[:80], "outcome": "degraded",
            },
        )

    def _build_system(self) -> str:
        """system prompt + few-shot 组装（模板来自 prompts/root_cause.<ver>.md，领域知识来自 pack）"""
        classes_block = "\n".join(f"- {k}: {v}" for k, v in self.pack.classes.items())
        system_prompt = self._prompt.render(
            role_intro=self.pack.role_intro,
            domain=self.pack.name.upper(),
            classes=classes_block,
            n_classes=len(self.pack.classes),
        )

        lines = [system_prompt]
        if self.pack.few_shot:
            lines.append("\n示例：")
            for msg in self.pack.few_shot:
                tag = "输入" if msg["role"] == "user" else "输出"
                lines.append(f"[{tag}] {msg['content']}")
        # 输出 schema 放在示例之后（T3-5）：模型对齐最后看到的格式，
        # 写在 few-shot 前面会被老 schema 的示例盖过去
        if self._prompt.post_example:
            lines.append(f"\n{self._prompt.post_example}")
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
