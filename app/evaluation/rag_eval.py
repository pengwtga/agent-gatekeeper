"""RAG 检索评测（T3-0：给检索质量一个可复现的数字）

动机（面试重点）：
    30 条知识库 + 0.55 采信阈值，以前都答不出"怎么定的"。
    本脚本把检索从"体感"变成"指标"：Recall@1 / Recall@3 / MRR，
    以及 rag_min_score 阈值扫描表 —— 阈值从"拍的"变成"在评测集上扫出来的"。

评测集：data/rag_queries.jsonl —— 30 个值班口吻的提问 → 应命中的 ticket_id。
    问题是对 symptom 的改写（不是原句复制），避免"自己出题自己答"。

用法：
    python -m app.rag_eval                # 关键词召回（离线，不调 LLM，秒出）
    python -m app.rag_eval --rerank       # 关键词召回 + LLM 重排（对齐线上降级模式）
    python -m app.rag_eval --sweep        # 采信阈值扫描表（仅关键词模式的分数有意义）

已知边界（写在明处）：
    - LLM 重排模式的 score 是排名近似（1.0 递减 0.1），不是语义相似度，
      阈值扫描对它没有意义 —— 这本身就是一个设计结论：
      rag_min_score 这道采信门禁只在"分数可解释"的通道上生效。
    - 评测集只有 30 条，样本量下单次评测波动大，看趋势不看小数点。
"""

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

from app.core.llm_client import llm_client
from app.agents.rag_assistant import RagAssistant

DATA_DIR = Path(__file__).parent.parent.parent / "data"  # app/evaluation/ → 项目根
KB_FILE = DATA_DIR / "kb_tickets.jsonl"
QUERY_FILE = DATA_DIR / "rag_queries.jsonl"
HARD_QUERY_FILE = DATA_DIR / "rag_queries_hard.jsonl"
TOP_K = 8  # MRR 的评估窗口，等于关键词宽召回量


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def rank_of_first_relevant(hits: list[dict], relevant: set[str]) -> int | None:
    """第一个命中条目在 top-K 里的排名（1 起）；没命中返回 None"""
    for i, h in enumerate(hits, 1):
        if h["ticket_id"] in relevant:
            return i
    return None


async def evaluate(queries: list[dict], rag: RagAssistant, use_rerank: bool) -> list[dict]:
    results = []
    for q in queries:
        relevant = set(q["relevant"])
        if use_rerank:
            # 宽召回 12 条再 LLM 重排回评估窗口 8 条；
            # 直接取 8 的话 `_llm_rerank` 会因 candidates<=top_k 跳过重排
            candidates = rag._keyword_search(q["question"], top_k=12)
            hits = await rag._llm_rerank(q["question"], candidates, top_k=TOP_K)
        else:
            hits = rag._keyword_search(q["question"], top_k=TOP_K)
        rank = rank_of_first_relevant(hits, relevant)
        results.append({
            "query_id": q["query_id"],
            "class": rag_class(rag, q["relevant"]),
            "rank": rank,
            "top1": hits[0]["ticket_id"] if hits else None,
            "top1_score": hits[0].get("score", 0) if hits else 0,
            "hits": [(h["ticket_id"], h.get("score", 0)) for h in hits],
        })
    return results


def rag_class(rag: RagAssistant, relevant: list[str]) -> str:
    """用 relevant 工单的根因类别做分组维度（每条查询 1~2 个相关工单）"""
    for t in rag.docs:
        if t["ticket_id"] == relevant[0]:
            return t["root_cause_class"]
    return "?"


def report(results: list[dict], tag: str) -> None:
    n = len(results)
    r1 = sum(1 for r in results if r["rank"] == 1)
    r3 = sum(1 for r in results if r["rank"] is not None and r["rank"] <= 3)
    rr = sum(1 / r["rank"] for r in results if r["rank"]) / n
    print(f"\n===== RAG 检索评测 [{tag} | {n} 条查询 | KB 30 条] =====")
    print(f"Recall@1: {r1}/{n} = {r1 / n:.1%}")
    print(f"Recall@3: {r3}/{n} = {r3 / n:.1%}")
    print(f"MRR@{TOP_K}: {rr:.3f}")

    # 按根因类别分组：暴露"哪一类检索得差"，而不是只给一个总分数
    by_class: dict[str, list] = defaultdict(list)
    for r in results:
        by_class[r["class"]].append(r)
    print("\n按根因类别分组（Recall@1 / Recall@3）：")
    for cls, items in sorted(by_class.items()):
        c1 = sum(1 for r in items if r["rank"] == 1)
        c3 = sum(1 for r in items if r["rank"] is not None and r["rank"] <= 3)
        print(f"  {cls:<22} {c1}/{len(items)} / {c3}/{len(items)}")

    misses = [r for r in results if r["rank"] is None]
    if misses:
        print("未命中样例（问题 → top1）：")
        for r in misses[:6]:
            print(f"  {r['query_id']} → {r['top1']}")


def sweep(results: list[dict]) -> None:
    """扫描采信阈值：每个阈值下的 查询覆盖 / 留存命中精度。

    两个指标合起来看：
    - coverage：多少查询在过滤后还留有结果（留得越少越容易"答不上来"）；
    - precision：留下的结果里有多少是对的（越高值班看到的垃圾越少）。
    阈值选在 precision 掉下来之前的最高 coverage 处。
    """
    print(f"\n===== 采信阈值 rag_min_score 扫描（关键词模式，{len(results)} 条）=====")
    print(f"{'阈值':>6} | {'查询覆盖':>10} | {'留存结果精度':>12}")
    for t in [0.0, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8]:
        covered, kept, kept_ok = 0, 0, 0
        for r in results:
            relevant = {tid for tid, _ in r["hits"]}  # hits 顺序即 top-K，逐条判断
            kept_hits = [tid for tid, s in r["hits"] if s >= t]
            if kept_hits:
                covered += 1
            kept += len(kept_hits)
            # 精度需要知道哪些 tid 是真正相关的，重查评测集
            kept_ok += sum(1 for tid in kept_hits if tid in r.get("relevant_set", set()))
        if t == 0.0:
            continue  # 全量保留无过滤意义，第一行由 0.3 起才有决策价值
        print(f"{t:>6} | {covered}/{len(results)} ({covered / len(results):>5.0%}) "
              f"| {kept_ok}/{kept}" if kept else f"{t:>6} | {covered}/{len(results)} | 0/0")


async def main() -> None:
    use_rerank = "--rerank" in sys.argv
    do_sweep = "--sweep" in sys.argv

    rag = RagAssistant(llm_client)
    rag.docs = load_jsonl(KB_FILE)
    rag._ready = True  # 不做 embedding 预热，纯离线评测

    tag = "关键词+LLM重排" if use_rerank else "关键词召回"
    for name, path in (("常规集", QUERY_FILE), ("困难集", HARD_QUERY_FILE)):
        if not path.exists():
            continue
        queries = load_jsonl(path)
        for q in queries:
            q["relevant_set"] = set(q["relevant"])
        results = await evaluate(queries, rag, use_rerank)
        for r, q in zip(results, queries):
            r["relevant_set"] = q["relevant_set"]
        report(results, f"{tag} | {name}")
        if do_sweep and not use_rerank and name == "常规集":
            sweep(results)


if __name__ == "__main__":
    asyncio.run(main())
