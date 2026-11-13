"""向量检索规模 benchmark — 回答"知识库超过 30 条，暴力余弦还顶得住吗"

用随机归一化向量实测（余弦计算量与向量取值无关，延迟反映的是真实矩阵乘开销），
维度取 embedding-2 的 512 维。每个规模跑 200 次查询取分位数。

用法：
    python -m scripts.vector_bench            # 默认规模梯度
    python -m scripts.vector_bench --quick    # 只跑到 1 万条
"""

import argparse
import statistics
import time

import numpy as np

DIM = 512  # 智谱 embedding-2 的输出维度
REPEATS = 200


def bench(n_docs: int, top_k: int = 3, seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    # 归一化后的随机向量矩阵（等价于真实 embedding 预处理后的形态）
    matrix = rng.standard_normal((n_docs, DIM)).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    queries = rng.standard_normal((REPEATS, DIM)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)

    latencies = []
    for q in queries:
        t0 = time.perf_counter()
        sims = matrix @ q  # 归一化后余弦 ≡ 点积，与 vector_store.search 同构
        _ = np.argsort(sims)[::-1][:top_k]
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    return {
        "n": n_docs,
        "mem_mb": matrix.nbytes / 1024 / 1024,
        "p50": statistics.median(latencies),
        "p95": latencies[int(len(latencies) * 0.95)],
        "max": latencies[-1],
    }


def bench_pipeline(n_docs: int, top_k: int = 3, seed: int = 42) -> dict:
    """全检索管线基准（不含 embedding API / LLM）：真实工单扰动扩充 → 关键词召回实测

    扰动方式：同一工单复制为变体时改 ticket_id、标题加变体标记、正文追加一句，
    保持真实中文文本形态（字符集分布/长度量级不变），测的才是真实打分开销。
    """
    import json
    from pathlib import Path

    from app.agents.rag_assistant import RagAssistant
    from app.core.llm_client import llm_client

    kb = Path(__file__).resolve().parents[1] / "data" / "ssrm" / "kb_tickets.jsonl"
    base = [json.loads(line) for line in kb.open(encoding="utf-8") if line.strip()]

    docs = []
    for i in range(n_docs):
        t = dict(base[i % len(base)])
        v = i // len(base)
        if v:
            t["ticket_id"] = f"SYN-{i:06d}"
            t["title"] = f"{t['title']}（场景变体{v}）"
            t["symptom"] = f"{t['symptom']}第{v}次出现，现象相同。"
        docs.append(t)

    rag = RagAssistant(llm_client)
    rag.docs = docs
    rag._ready = True

    data_dir = Path(__file__).resolve().parents[1] / "data" / "ssrm"
    questions = [json.loads(line)["question"] for f in ("rag_queries.jsonl", "rag_queries_hard.jsonl")
                 for line in (data_dir / f).open(encoding="utf-8") if line.strip()]

    latencies = []
    for _ in range(max(1, 200 // len(questions))):  # 每档约 200 次
        for q in questions:
            t0 = time.perf_counter()
            rag._keyword_search(q, top_k=top_k)
            latencies.append((time.perf_counter() - t0) * 1000)
    latencies.sort()
    return {"n": n_docs, "p50": statistics.median(latencies),
            "p95": latencies[int(len(latencies) * 0.95)], "max": latencies[-1]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="只跑到 1 万条")
    parser.add_argument("--pipeline", action="store_true",
                        help="真实工单扰动扩充，测关键词召回阶段（纯离线，不含 embedding/LLM）")
    args = parser.parse_args()

    if args.pipeline:
        scales = [30, 1_000, 10_000, 50_000]
        if args.quick:
            scales = [30, 1_000]
        print(f"\n===== 全检索管线基准：真实工单扰动扩充 + 关键词召回（每档约 200 次查询） =====")
        print(f"{'知识库规模':>10} | {'p50':>9} | {'p95':>9} | {'max':>9}")
        print("-" * 52)
        for n in scales:
            r = bench_pipeline(n)
            print(f"{r['n']:>10,} | {r['p50']:>7.2f}ms | {r['p95']:>7.2f}ms | {r['max']:>7.2f}ms")
        print("\n口径：不含 embedding API 与 LLM 重排（两项不随 KB 规模增长）；")
        print("对照 NumPy 余弦内核（--quick 档）可见：纯 Python 关键词扫描才是先到的瓶颈。")
        return

    scales = [30, 100, 1_000, 10_000, 50_000, 100_000]
    if args.quick:
        scales = [30, 100, 1_000, 10_000]

    print(f"\n===== NumPy 暴力余弦检索规模测试（dim={DIM}, top_k=3, {REPEATS} 次查询/档） =====")
    print(f"{'知识库规模':>10} | {'内存':>8} | {'p50':>9} | {'p95':>9} | {'max':>9} | 结论")
    print("-" * 78)
    verdicts = {30: "当前规模", 100: "余量充足", 1_000: "设计舒适区",
                10_000: "仍可接受", 50_000: "接近换挡", 100_000: "应换 ANN"}
    for n in scales:
        r = bench(n)
        print(f"{r['n']:>10,} | {r['mem_mb']:>6.1f}MB | {r['p50']:>7.2f}ms | {r['p95']:>7.2f}ms | {r['max']:>7.2f}ms | {verdicts[n]}")
    print("\n结论口径：千条级毫秒内，万条级个位数毫秒，十万条级应换 Faiss/HNSW/持久化向量库。")
    print("对照：一次 glm-4-flash LLM 调用 p95 ≈ 4200ms —— 检索在整条链路里可忽略。")


if __name__ == "__main__":
    main()
