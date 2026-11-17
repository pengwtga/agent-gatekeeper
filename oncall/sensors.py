"""验收传感器：agent 说「我完成了」之后，先过这一关再交给人。

I1 只做两道计算型（能算的绝不问模型）：
- S1 结论非空且可执行
- S2 证据覆盖 ≥2 个**不同字段**（不是 ≥2 条）

S2 的字段去重是从 v1 学来的：只数条数时，同一句日志切两段就能凑满分。
要求「不同字段」等于逼它从两个独立来源取证（日志 + 配置、指标 + 变更）。

S3（ref 可追溯）/ S4（动作安全）/ S5（模型判证据是否支持结论）在 I2/I4 接。
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

MIN_CONCLUSION_CHARS = 10
MIN_EVIDENCE_FIELDS = 2

_VAGUE_PREFIXES = ("可能", "大概", "也许", "或许是", "不太确定")


@dataclass
class SensorReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    evidence_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "evidence_fields": self.evidence_fields}


def _distinct_fields(evidence: Iterable[dict]) -> list[str]:
    fields: list[str] = []
    for item in evidence:
        name = str(item.get("field", "")).strip()
        if name and name not in fields:
            fields.append(name)
    return fields


def check_finish(conclusion: str, evidence: Iterable[dict], *,
                 requires_cross_source: bool = True) -> SensorReport:
    """`requires_cross_source` 只在「结论断言了根因」时为 True。

    为什么不一刀切要求两个来源：问「知识库有多少条」时，唯一权威来源就是 kb_stats，
    再逼它凑第二个字段只能靠编。跨源印证要留给**有风险的断言**（根因判定），
    否则传感器会变成「为了过检查而编证据」的激励。
    """
    report = SensorReport()
    evidence = list(evidence or [])

    text = (conclusion or "").strip()
    if len(text) < MIN_CONCLUSION_CHARS:
        report.errors.append(f"S1 结论过短或为空（{len(text)} 字）")
    elif text.startswith(_VAGUE_PREFIXES):
        # 模糊开头只是提醒：有些结论本身就得带不确定性，不能一刀切拦掉
        report.warnings.append("S1 结论以模糊词开头，人工复核时注意")

    missing = [
        i for i, item in enumerate(evidence)
        if not str(item.get("ref", "")).strip() or not str(item.get("quote", "")).strip()
    ]
    if missing:
        report.errors.append(f"S3 证据缺 ref/quote：第 {missing} 条")

    report.evidence_fields = _distinct_fields(evidence)
    if requires_cross_source and len(report.evidence_fields) < MIN_EVIDENCE_FIELDS:
        report.errors.append(
            f"S2 证据只覆盖 {len(report.evidence_fields)} 个字段（要求 ≥{MIN_EVIDENCE_FIELDS}）："
            f"{report.evidence_fields}"
        )

    report.ok = not report.errors
    return report
