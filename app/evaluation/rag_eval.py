"""RAG 检索评测（T3-0：给检索质量一个可复现的数字）

动机（面试重点）：
    知识库规模 + 采信阈值，以前都答不出"怎么定的"。
    本脚本把检索从"体感"变成"指标"：Recall@1 / Recall@3 / MRR，
    以及采信阈值扫描表 —— 阈值从"拍的"变成"在评测集上扫出来的"。

评测集：每个领域一组「口吻化提问 → 应命中的知识库条目」。
    问题是对 symptom 的改写（不是原句复制），避免"自己出题自己答"。

用法：
    python -m app.evaluation.rag_eval                        # SSRM 域（默认）
    python -m app.evaluation.rag_eval --domain csa           # 坐席助手域
    python -m app.evaluation.rag_eval --domain credit        # 催员辅助域
    python -m app.evaluation.rag_eval --rerank               # 关键词召回 + LLM 重排
    python -m app.evaluation.rag_eval --sweep                # 采信阈值扫描（仅 ssrm 常规集）

已知边界（写在明处）：
    - LLM 重排模式的 score 是排名近似（1.0 递减 0.1），不是语义相似度，
      阈值扫描对它没有意义 —— 这本身就是一个设计结论：
      采信阈值这道门禁只在"分数可解释"的通道上生效。
"""

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

from app.core.llm_client import llm_client
from app.agents.rag_assistant import RagAssistant
from app.domains import AssistantSpec
from app.domains.csa import pack as csa_pack
from app.domains.credit import pack as credit_pack
from app.domains.ssrm import pack as ssrm_pack

DATA_DIR = Path(__file__).parent.parent.parent / "data"  # app/evaluation/ → 项目根
TOP_K = 8  # MRR 的评估窗口，等于关键词宽召回量

# 领域 → (助手规格, 评测查询集)。规格与 pack 同源：引擎、知识库、阈值一处声明
_DOMAINS: dict[str, tuple[AssistantSpec, list[tuple[str, Path]]]] = {
    "ssrm": (ssrm_pack.assistant, [
        ("常规集", DATA_DIR / "ssrm" / "rag_queries.jsonl"),
        ("困难集", DATA_DIR / "ssrm" / "rag_queries_hard.jsonl"),
    ]),
    "csa": (csa_pack.assistant, [("坐席助手检索", DATA_DIR / "csa" / "csa_kb_queries.jsonl")]),
    "credit": (credit_pack.assistant, [("催员辅助检索", DATA_DIR / "credit" / "credit_kb_queries.jsonl")]),
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def build_cluster_map(docs: list[dict], id_field: str) -> tuple[dict[str, str], dict[str, set[str]]]:
    """现象簇的双向映射：(条目 id → 簇 id, 簇 id → 全簇条目集合)。

    SSRM 的变体（ONCALL-001-V2）与母本（ONCALL-001）是同现象同处置的同一簇；
    其他领域（csa/credit）每条唯一，簇 = 自身。扩容后的检索评测必须分两个口径：
    strict（精确命中标注 id）与 cluster（命中同现象簇任一条目）——同现象案例对用户
    同样有用，两口径之差就是「关键词字符打分的簇内区分度」，它是选型信号而非噪音。
    """
    to_cluster = {d[id_field]: d.get("cluster_key") or d[id_field].split("-V")[0]
                  for d in docs}
    to_members: dict[str, set[str]] = {}
    for tid, base in to_cluster.items():
        to_members.setdefault(base, set()).add(tid)
    return to_cluster, to_members


def rank_of_first_relevant(hits: list[dict], relevant: set[str], id_field: str) -> int | None:
    """第一个命中条目在 top-K 里的排名（1 起）；没命中返回 None"""
    for i, h in enumerate(hits, 1):
        if h[id_field] in relevant:
            return i
    return None


async def evaluate(queries: list[dict], rag: RagAssistant, use_rerank: bool,
                   cluster: dict[str, str], cluster_members: dict[str, set[str]]) -> list[dict]:
    id_field = rag.spec.id_field
    results = []
    for q in queries:
        relevant = set(q["relevant"])
        q_cluster = next(iter({cluster.get(t, t) for t in relevant}))
        cluster_relevant = cluster_members.get(q_cluster, {q_cluster})
        if use_rerank:
            # 宽召回 12 条再 LLM 重排回评估窗口 8 条；
            # 直接取 8 的话 `_llm_rerank` 会因 candidates<=top_k 跳过重排
            candidates = rag._keyword_search(q["question"], top_k=12)
            hits = await rag._llm_rerank(q["question"], candidates, top_k=TOP_K)
        else:
            hits = rag._keyword_search(q["question"], top_k=TOP_K)
        results.append({
            "query_id": q["query_id"],
            "class": rag_class(rag, q["relevant"]),
            "rank": rank_of_first_relevant(hits, relevant, id_field),
            "rank_c": rank_of_first_relevant(hits, cluster_relevant, id_field),
            "top1": hits[0][id_field] if hits else None,
            "top1_score": hits[0].get("score", 0) if hits else 0,
            "hits": [(h[id_field], h.get("score", 0)) for h in hits],
            "cluster_relevant": cluster_relevant,
        })
    return results


def rag_class(rag: RagAssistant, relevant: list[str]) -> str:
    """用 relevant 条目的类别字段做分组维度（每条查询 1~2 个相关条目）"""
    id_f, class_f = rag.spec.id_field, rag.spec.class_field
    for t in rag.docs:
        if t[id_f] == relevant[0]:
            return t[class_f]
    return "?"


def report(results: list[dict], tag: str, kb_count: int) -> None:
    n = len(results)

    def _stat(key: str) -> tuple[int, int, float]:
        r1 = sum(1 for r in results if r[key] == 1)
        r3 = sum(1 for r in results if r[key] is not None and r[key] <= 3)
        rr = sum(1 / r[key] for r in results if r[key]) / n
        return r1, r3, rr

    print(f"\n===== RAG 检索评测 [{tag} | {n} 条查询 | KB {kb_count} 条] =====")
    for key, label in (("rank_c", "簇级（同现象即命中）"), ("rank", "严格（精确命中标注 id）")):
        r1, r3, rr = _stat(key)
        print(f"[{label}]")
        print(f"  Recall@1: {r1}/{n} = {r1 / n:.1%} | Recall@3: {r3}/{n} = {r3 / n:.1%} | MRR@{TOP_K}: {rr:.3f}")

    # 按类别分组：暴露哪一类检索得差，而不是只给一个总分数
    by_class: dict[str, list] = defaultdict(list)
    for r in results:
        by_class[r["class"]].append(r)
    print("\n按类别分组（簇级 Recall@1 / Recall@3）：")
    for cls, items in sorted(by_class.items()):
        c1 = sum(1 for r in items if r["rank_c"] == 1)
        c3 = sum(1 for r in items if r["rank_c"] is not None and r["rank_c"] <= 3)
        print(f"  {cls:<22} {c1}/{len(items)} / {c3}/{len(items)}")

    misses = [r for r in results if r["rank_c"] is None]
    if misses:
        print("簇级也未命中样例（问题 → top1）：")
        for r in misses[:6]:
            print(f"  {r['query_id']} → {r['top1']}")


def sweep(results: list[dict]) -> None:
    """扫描采信阈值（簇级口径）：每个阈值下的 查询覆盖 / 留存命中精度。

    两个指标合起来看：
    - coverage：多少查询在过滤后还留有结果（留得越少越容易答不上来）；
    - precision：留下的结果里有多少是同现象簇的（越高用户看到的垃圾越少）。
    阈值选在 precision 掉下来之前的最高 coverage 处。
    """
    print(f"\n===== 采信阈值扫描（关键词模式，簇级口径，{len(results)} 条）=====")
    print(f"{'阈值':>6} | {'查询覆盖':>10} | {'留存结果精度':>12}")
    for t in [0.0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8]:
        covered, kept, kept_ok = 0, 0, 0
        for r in results:
            kept_hits = [tid for tid, s2 in r["hits"] if s2 >= t]
            if kept_hits:
                covered += 1
            kept += len(kept_hits)
            kept_ok += sum(1 for tid in kept_hits if tid in r["cluster_relevant"])
        if t == 0.0:
            continue  # 全量保留无过滤意义，第一行由 0.3 起才有决策价值
        print(f"{t:>6} | {covered}/{len(results)} ({covered / len(results):>5.0%}) "
              f"| {kept_ok}/{kept}" if kept else f"{t:>6} | {covered}/{len(results)} | 0/0")


async def main() -> None:
    use_rerank = "--rerank" in sys.argv
    do_sweep = "--sweep" in sys.argv
    domain = "ssrm"
    if "--domain" in sys.argv:
        domain = sys.argv[sys.argv.index("--domain") + 1]
    if domain not in _DOMAINS:
        raise SystemExit(f"未知领域 {domain!r}，可选：{list(_DOMAINS)}")

    spec, query_sets = _DOMAINS[domain]
    rag = RagAssistant(llm_client, spec)
    rag.docs = load_jsonl(spec.kb_file)
    rag._ready = True  # 不做 embedding 预热，纯离线评测
    cluster, cluster_members = build_cluster_map(rag.docs, spec.id_field)

    tag = "关键词+LLM重排" if use_rerank else "关键词召回"
    for name, path in query_sets:
        if not path.exists():
            continue
        queries = load_jsonl(path)
        results = await evaluate(queries, rag, use_rerank, cluster, cluster_members)
        report(results, f"{domain} | {tag} | {name}", len(rag.docs))
        if do_sweep and not use_rerank and name == query_sets[0][0]:
            sweep(results)


if __name__ == "__main__":
    asyncio.run(main())
