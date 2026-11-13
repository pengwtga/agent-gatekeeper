"""评测脚本（命令行版）

用法：
    python -m app.evaluation.eval [limit]                    # SSRM 常规集，LLM 通道
    python -m app.evaluation.eval 30 --rule                  # 强制规则基线
    python -m app.evaluation.eval 30 --hard                  # 困难集
    python -m app.evaluation.eval 30 --domain csa            # 电商客服领域（同引擎换包）
    python -m app.evaluation.eval 30 --domain csa --rule     # 客服领域规则基线
"""

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

from app.agents.classifier import RootCauseClassifier
from app.core.llm_client import llm_client
from app.domains import DomainPack
from app.domains.csa import pack as csa_pack
from app.domains.credit import pack as credit_pack
from app.domains.ssrm import pack as ssrm_pack
from app.evaluation.dataset import load_dataset

DATA_DIR = Path(__file__).parent.parent.parent / "data"  # app/evaluation/ → 项目根

# 领域注册表：评测入口按 --domain 选包。接新领域 = 这里加一行 + 写一个 pack
_DOMAINS: dict[str, tuple[DomainPack, Path]] = {
    "ssrm": (ssrm_pack, DATA_DIR / "ssrm" / "incidents.jsonl"),
    "csa": (csa_pack, DATA_DIR / "csa" / "csa_tickets.jsonl"),
    "credit": (credit_pack, DATA_DIR / "credit" / "credit_applications.jsonl"),
}


async def main(limit: int, use_rule: bool, hard: bool, domain: str):
    pack, default_path = _DOMAINS[domain]
    # hard 集目前只有 ssrm 有；csa 的困难样本直接混在常规集里（口语化改写）
    path = (DATA_DIR / "ssrm" / "incidents_hard.jsonl" if hard else default_path) if domain == "ssrm" else default_path
    classifier = RootCauseClassifier(llm_client, pack)

    async def classify(row):
        if use_rule:
            result = classifier._rule_baseline(row["incident"])
            result["source"] = "rule_forced"
            return result
        return await classifier.classify(row["incident"])

    rows = load_dataset(limit=limit, shuffle=True, path=path)
    correct, latencies, sources = 0, [], Counter()
    confusions, unknowns = [], 0

    for row in rows:
        t0 = time.monotonic()
        result = await classify(row)
        latencies.append((time.monotonic() - t0) * 1000)
        sources[result.get("source", "?")] += 1
        pred = result["root_cause_class"]
        label = row["label"]
        if pred == label:
            correct += 1
        else:
            rid = row["incident"].get("incident_id") or row["incident"].get("ticket_id") or row["incident"].get("app_id", "?")
            confusions.append((label, pred, rid))
        if pred == "unknown":
            unknowns += 1

    total = len(rows)
    tag = ("困难集" if hard else "常规集") if domain == "ssrm" else f"{domain} 全量"
    channel = "规则基线(强制)" if use_rule else "LLM 容灾链"
    print(f"\n===== {domain.upper()} 分类评测 [{tag} | {channel}] =====")
    print(f"样本数: {total} | 通道: {dict(sources)}")
    print(f"总体准确率: {correct}/{total} = {correct / total:.1%}")
    print(f"unknown 判定: {unknowns}/{total}（宁可说不知道的笔数；线上它驱动新类别的发现）")
    print(f"平均延迟: {sum(latencies) / len(latencies):.0f} ms")
    if confusions:
        print("混淆样例（label → predicted）:")
        for label, pred, iid in confusions[:8]:
            print(f"  {iid}: {label} → {pred}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = int(args[0]) if args else 30
    use_rule = "--rule" in sys.argv
    hard = "--hard" in sys.argv
    domain = "ssrm"
    for a in sys.argv[1:]:
        if a.startswith("--domain="):
            domain = a.split("=", 1)[1]
        elif a == "--domain" and a != sys.argv[-1]:
            nxt = sys.argv[sys.argv.index(a) + 1]
            if not nxt.startswith("--"):
                domain = nxt
    if domain not in _DOMAINS:
        raise SystemExit(f"未知领域: {domain}，可选: {list(_DOMAINS)}")
    asyncio.run(main(limit, use_rule, hard, domain))
