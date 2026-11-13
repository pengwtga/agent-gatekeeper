"""评测脚本（命令行版）

用法：
    python -m app.evaluation.eval [limit]                    # SSRM 常规集，LLM 通道
    python -m app.evaluation.eval 30 --rule                  # 强制规则基线
    python -m app.evaluation.eval 30 --hard                  # 困难集
    python -m app.evaluation.eval --regression               # 20 条手工回归集（T3-2，全量跑）
    python -m app.evaluation.eval --regression --record      # 回归结果写入 prompts/scores.json
    python -m app.evaluation.eval --regression --prompt-version=v1   # 用 v1 prompt 跑（A/B 对比）
    python -m app.evaluation.eval 30 --domain csa            # 电商客服领域（同引擎换包）
    python -m app.evaluation.eval 30 --domain csa --rule     # 客服领域规则基线
"""

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

from app import prompts as prompt_store
from app.agents.classifier import RootCauseClassifier
from app.config import settings
from app.core.llm_client import llm_client
from app.domains import DomainPack
from app.domains.credit import pack as credit_pack
from app.domains.csa import pack as csa_pack
from app.domains.ssrm import pack as ssrm_pack
from app.evaluation.dataset import load_dataset
from app.prompts import record_score

DATA_DIR = Path(__file__).parent.parent.parent / "data"  # app/evaluation/ → 项目根
REGRESSION_FILE = DATA_DIR / "ssrm" / "regression.jsonl"  # T3-2：20 条手工回归集（5 类 × 4）

# 领域注册表：评测入口按 --domain 选包。接新领域 = 这里加一行 + 写一个 pack
_DOMAINS: dict[str, tuple[DomainPack, Path]] = {
    "ssrm": (ssrm_pack, DATA_DIR / "ssrm" / "incidents.jsonl"),
    "csa": (csa_pack, DATA_DIR / "csa" / "csa_tickets.jsonl"),
    "credit": (credit_pack, DATA_DIR / "credit" / "credit_applications.jsonl"),
}


async def main(limit: int, use_rule: bool, hard: bool, domain: str,
               regression: bool = False, record: bool = False, prompt_version: str = ""):
    # A/B：不动 index.json 的 active 指针，只在本进程内改覆盖值 —— 评测完仓库文件还是干净的
    if prompt_version:
        settings.prompts_active = {"root_cause": prompt_version}
        prompt_store.reload()
    pack, default_path = _DOMAINS[domain]
    # hard 集目前只有 ssrm 有；csa 的困难样本直接混在常规集里（口语化改写）
    if regression:
        path, tag = REGRESSION_FILE, "回归集"
        limit = 0  # 回归集只有 20 条，全跑
    elif hard:
        path, tag = DATA_DIR / "ssrm" / "incidents_hard.jsonl", "困难集"
    else:
        path, tag = default_path, ("常规集" if domain == "ssrm" else f"{domain} 全量")
    classifier = RootCauseClassifier(llm_client, pack)

    async def classify(row):
        if use_rule:
            result = classifier._rule_baseline(row["incident"])
            result["source"] = "rule_forced"
            return result
        return await classifier.classify(row["incident"])

    rows = load_dataset(limit=limit, shuffle=True, path=path)
    correct, latencies, sources = 0, [], Counter()
    confusions, unknowns, probes = [], 0, []
    modes, refs_counts, blocked = Counter(), [], 0
    blocked_samples = []

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
        # 证据判据的采用情况（T3-5）：refs 采用率低 = 模型没按 schema 输出，正在走兜底
        if "evidence_mode" in result:
            modes[result["evidence_mode"]] += 1
            refs_counts.append(result.get("evidence_refs_count", 0))
            if result.get("evidence_warning"):
                blocked += 1
                blocked_samples.append((row["incident"].get("incident_id")
                                        or row["incident"].get("ticket_id")
                                        or row["incident"].get("app_id", "?"),
                                        result.get("evidence_score", -1.0),
                                        result.get("evidence_mode", "?")))
        # 证据探针（T3-2）：只关心「证据门禁有没有误伤诚实样本」
        if row.get("evidence_probe") and "evidence_score" in result:
            probes.append((row["incident"]["incident_id"], result["evidence_score"],
                           result.get("needs_human_review"), row["evidence_probe"]["kind"],
                           result.get("evidence_mode", "?")))

    total = len(rows)
    channel = "规则基线(强制)" if use_rule else "LLM 容灾链"
    prompt_label = getattr(classifier, "_prompt", None)
    prompt_label = prompt_label.label if prompt_label else "-"
    print(f"\n===== {domain.upper()} 分类评测 [{tag} | {channel}] =====")
    print(f"样本数: {total} | 通道: {dict(sources)} | prompt: {prompt_label}")
    print(f"总体准确率: {correct}/{total} = {correct / total:.1%}")
    print(f"unknown 判定: {unknowns}/{total}（宁可说不知道的笔数；线上它驱动新类别的发现）")
    print(f"平均延迟: {sum(latencies) / len(latencies):.0f} ms")
    if modes:
        # refs 采用率是「schema 变更是否被模型接受」的直接证据，比准确率更能说明这次改造的落地程度
        adopted = modes.get("refs", 0) / sum(modes.values())
        avg_refs = sum(refs_counts) / len(refs_counts) if refs_counts else 0
        print(f"证据判据: {dict(modes)} | refs 采用率 {adopted:.0%} | 平均引用 {avg_refs:.1f} 条 | 被门禁拦下 {blocked}")
        # 被拦下的明细必须打出来：门禁误杀诚实样本是这套机制的主要风险，
        # 只看「拦了几条」看不出是「拦对了编造」还是「拦错了诚实」
        for iid, score, mode in blocked_samples:
            print(f"  拦下 {iid}: score={score:.3f} mode={mode}")
    if probes:
        for iid, score, review, kind, mode in probes:
            verdict = "PASS" if not review else "GATE_BLOCKED"
            print(f"证据探针 {iid}({kind}): evidence_score={score:.3f} mode={mode} → {verdict}")
    if confusions:
        print("混淆样例（label → predicted）:")
        for label, pred, iid in confusions[:8]:
            print(f"  {iid}: {label} → {pred}")

    # 可对比的一行摘要（T3-3）：make eval 把多次运行并排放，一眼看版本差异
    acc = correct / total
    print(f"[{prompt_label}] {domain}/{tag}: {acc:.2f}")
    if record and not use_rule and domain == "ssrm":
        metric = "regression" if regression else ("hard" if hard else "normal")
        path_written = record_score("root_cause", classifier._prompt.version, metric, acc)
        print(f"已记录分数 → {path_written.relative_to(DATA_DIR.parent)}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = int(args[0]) if args else 30
    use_rule = "--rule" in sys.argv
    hard = "--hard" in sys.argv
    regression = "--regression" in sys.argv
    record = "--record" in sys.argv
    domain = "ssrm"
    prompt_version = ""
    for a in sys.argv[1:]:
        if a.startswith("--domain="):
            domain = a.split("=", 1)[1]
        elif a.startswith("--prompt-version="):
            prompt_version = a.split("=", 1)[1]
        elif a == "--prompt-version" and a != sys.argv[-1]:
            nxt = sys.argv[sys.argv.index(a) + 1]
            if not nxt.startswith("--"):
                prompt_version = nxt
        elif a == "--domain" and a != sys.argv[-1]:
            nxt = sys.argv[sys.argv.index(a) + 1]
            if not nxt.startswith("--"):
                domain = nxt
    if domain not in _DOMAINS:
        raise SystemExit(f"未知领域: {domain}，可选: {list(_DOMAINS)}")
    asyncio.run(main(limit, use_rule, hard, domain, regression, record, prompt_version))
