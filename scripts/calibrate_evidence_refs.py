"""结构化引用判据的标定（T3-5）

回答一个问题：换成 `evidence_refs` 之后，诚实样本和编造样本的分数还分得开吗？

用的是 T0-03 那 10 条手工标定样本的**结构化版本**——同一批 incident、同一批判断意图，
只是把「一段话」改写成了「引用列表」。这样才能回答「换判据是不是真的解决了分数反转」，
而不是换一批更好看的样本。

判据（`app/agents/evidence.py::check_refs`）：字段存在吗？值相等吗？得分 = 有效引用/总引用。
门禁有**两个**条件（都在 settings 里）：

- `min_evidence_refs_score = 0.7`：有效引用占比
- `min_evidence_refs = 2`：引用条数下限

第二条专门堵「少说少错」：只丢一个正确字段也能拿 1.0 分，那是虚高，不是证据质量。
T0-03 里的 J（`EA_TIMEOUT / 504` 得 1.000 > A 详述得 0.684）就是这条漏洞的极端表现。

用法：python scripts/calibrate_evidence_refs.py
退出口径：诚实样本必须 ≥0.9 且未被门禁拦；纯编造必须 ≤0.2 且被门禁拦。任一不满足 → exit 1。
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.classifier import RootCauseClassifier  # noqa: E402
from app.core.llm_client import llm_client  # noqa: E402
from app.domains.ssrm import pack  # noqa: E402

# 门禁拦截时会打 WARNING（线上要留痕），标定时会把表格冲散，这里只静默日志不改行为
logging.getLogger("app.agents.classifier").setLevel(logging.ERROR)

# 标定用的输入：这条 incident 原文见 docs/optimization/T0-03 样本 A
INCIDENT = {
    "stage": "register",
    "source_system": "ssrm",
    "error_code": "EA_TIMEOUT",
    "http_status": 504,
    "retry_count": 4,
    "downstream": "ea",
    "wait_minutes": 17,
    "error_message": "EA updateSupplier 超时，等待 504 响应，重试 4 次均失败",
    "incident_id": "CAL-01",
    "hour": 10,
}

APPROVED_INCIDENT = {  # 样本 B 的输入：审批已通过但下游无任务，没有错误码
    "stage": "register",
    "source_system": "xflow",
    "error_code": None,
    "http_status": None,
    "retry_count": 0,
    "downstream": "ssrm",
    "wait_minutes": 42,
    "error_message": "审批单 FT20261023001 状态为 APPROVED，但 SSRM 无对应注册任务",
    "incident_id": "CAL-02",
    "hour": 12,
}

DUPLICATE_INCIDENT = {  # 样本 E 的输入：幂等冲突
    "stage": "change",
    "source_system": "dora",
    "error_code": "DUPLICATE_KEY",
    "http_status": 409,
    "retry_count": 0,
    "downstream": "ssrm",
    "wait_minutes": 1,
    "error_message": "重复提交：供应商注册申请 SUP874634 已存在（幂等冲突）",
    "incident_id": "CAL-03",
    "hour": 10,
}

# (编号, 类型, 是否应当被门禁拦下, incident, refs, evidence 自由文本)
CASES = [
    ("A", "诚实-引用三个编码字段", False, INCIDENT,
     [{"field": "error_code", "value": "EA_TIMEOUT"},
      {"field": "http_status", "value": 504},
      {"field": "retry_count", "value": 4}],
     "error_code=EA_TIMEOUT 且 http_status=504，重试 4 次均失败，指向 EA 侧超时"),
    ("B", "诚实-无错误码，引流程字段", False, APPROVED_INCIDENT,
     [{"field": "stage", "value": "register"},
      {"field": "source_system", "value": "xflow"},
      {"field": "wait_minutes", "value": 42}],
     "审批单已通过但 SSRM 侧没有对应注册任务，说明回调未到达"),
    ("E", "诚实-引用状态码", False, DUPLICATE_INCIDENT,
     [{"field": "http_status", "value": 409},
      {"field": "error_code", "value": "DUPLICATE_KEY"}],
     "http_status=409 且提示申请已存在，判定为重复提交"),
    ("J", "诚实但只引一条（原 1.000 反转源）", True, INCIDENT,
     [{"field": "error_code", "value": "EA_TIMEOUT"}],
     "EA_TIMEOUT / 504"),
    ("G", "半编造-引真码、结论过度", False, INCIDENT,
     [{"field": "error_code", "value": "EA_TIMEOUT"},
      {"field": "http_status", "value": 504}],
     "error_code=EA_TIMEOUT，说明 EA 服务整体不可用，建议联系 EA 团队"),
    ("H", "纯编造-通用词", True, INCIDENT,
     [{"field": "network_jitter", "value": True},
      {"field": "rtt_ms", "value": 120}],
     "网络抖动导致调用超时，建议直接重试"),
    ("C", "纯编造-泛化", True, INCIDENT,
     [{"field": "network_status", "value": "abnormal"},
      {"field": "latency_p99", "value": 2000}],
     "看起来是网络问题导致供应商数据同步异常"),
    ("D", "纯编造-换话题", True, INCIDENT,
     [{"field": "db_pool_usage", "value": 0.98},
      {"field": "active_connections", "value": 200}],
     "系统整体负载较高，可能是数据库连接池耗尽"),
    ("F", "纯编造-空话", True, INCIDENT,
     [{"field": "known_issue", "value": True},
      {"field": "runbook", "value": "standard"}],
     "根据经验判断，这是一个已知问题，按标准流程处理即可"),
    ("I", "纯编造-套模板", True, INCIDENT,
     [{"field": "downstream_status", "value": "unstable"},
      {"field": "history_similar", "value": 3}],
     "根据历史经验，此类问题多为下游系统不稳定，建议转人工处理"),
    ("K", "编造-换领域", True, INCIDENT,
     [{"field": "license_status", "value": "missing"},
      {"field": "qualification", "value": "expired"}],
     "供应商资质审核不通过，需要补充营业执照"),
    ("X", "编造-字段真实但值被改", True, INCIDENT,
     [{"field": "error_code", "value": "EA_REFUSED"},
      {"field": "retry_count", "value": 9}],
     "EA 明确拒绝了本次同步请求"),
]


def main() -> int:
    classifier = RootCauseClassifier(llm_client, pack)
    failures: list[str] = []

    print(f"{'#':2s} {'类型':28s} {'score':>6s} {'n':>2s} {'mode':7s} {'conf':>5s} {'门禁':6s}")
    print("-" * 74)
    for case_id, kind, should_block, incident, refs, evidence in CASES:
        # 造一个「模型原始输出」，再走真实门禁 —— 标定必须打在线上真正执行的那段代码上
        result = {
            "root_cause_class": "ea_sync_timeout",
            "confidence": 0.95,
            "evidence": evidence,
            "evidence_refs": refs,
            "suggested_action": "占位",
        }
        classifier._apply_evidence_gate(result, incident)
        blocked = bool(result.get("evidence_warning"))
        score = result.get("evidence_score", -1.0)
        verdict = "拦截" if blocked else "放行"

        print(f"{case_id:2s} {kind:28s} {score:6.3f} {len(result.get('evidence_refs') or []):2d} "
              f"{result.get('evidence_mode'):7s} {result['confidence']:5.2f} {verdict:6s}")

        if blocked != should_block:
            failures.append(f"{case_id}（{kind}）门禁决策错误：期望{'拦截' if should_block else '放行'}")

    # 分类型汇总：诚实样本分高、纯编造分低，这才是判据有效的证据
    def scores(keyword: str) -> list[float]:
        out = []
        for case_id, kind, _, incident, refs, evidence in CASES:
            if keyword in kind:
                result = {"root_cause_class": "ea_sync_timeout", "confidence": 0.95,
                          "evidence": evidence, "evidence_refs": refs}
                classifier._apply_evidence_gate(result, incident)
                out.append(result["evidence_score"])
        return out

    honest = scores("诚实")
    fabricated = scores("编造")
    print("-" * 74)
    print(f"诚实样本 score: {honest} → 最低 {min(honest):.3f}")
    print(f"编造样本 score（含半编造）: {fabricated} → 最高 {max(fabricated):.3f}")

    if min(honest) < 0.9:
        failures.append(f"诚实样本最低分 {min(honest):.3f} < 0.9")
    pure = [s for s in fabricated if s <= 0.2]
    if len(pure) < len(fabricated) - 1:  # 只允许 G 这类「引真码结论过度」通过
        failures.append(f"纯编造样本最低分 {min(fabricated):.3f}，超过 0.2 的过多")

    print()
    print("说明：")
    print("  G 属于「引用正确但结论过度」，任何引用校验都抓不到（那是蕴含判断），两条边界一致，")
    print("  不列为失败项；它证明的是这条能力的上限，不是回归 bug。")
    print("  J 的分数仍是 1.000（单条引用全中），但它被『引用条数下限』拦下了 ——")
    print("  反转不是靠压分消除的，是靠加一个独立条件，这点比调权重更经得起追问。")

    if failures:
        for f in failures:
            print(f"\nFAIL {f}")
        return 1
    print("\nPASS 标定口径全部满足")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
