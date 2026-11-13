"""RAG 助手引擎 — 领域无关：历史案例检索 + LLM 处置建议

机制与知识分离（面试重点）：
引擎只负责机制——宽召回 + LLM 重排 / 向量检索、采信阈值门禁、grounding 总结、
双级降级；「知识库里放什么、阈值多少、助手的人设边界」由领域的 AssistantSpec 声明
（ssrm=值班助手 / csa=坐席助手 / credit=催员辅助），同一引擎服务三个领域。

流程（以 SSRM 值班助手为例）：
1. 知识库预热：历史工单 → 智谱 embedding-2 向量化 → 内存向量库（JSONL 持久化）
2. 提问：问题向量化 → 余弦 Top-K 与字符 bigram Top-K 做 RRF 混合排序（T4-4）→
   相似 case 作为上下文 → LLM 总结处置建议；embedding 不可用时走关键词 + LLM 重排
3. 双级降级：embedding 不可用 → 关键词检索 + LLM 重排兜底；LLM 不可用 → 直接返回原始 case
4. 采信阈值：分数 < 阈值（spec.threshold）的检索结果标记"低置信"，提示人工判断
   （T4-6：分数始终为可解释口径——vector 模式是余弦相似度，keyword 模式是字符
   bigram 覆盖率，每条命中带 match_type 标注来源；重排不再伪造排名分）
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from app.core.budget import Budget
from app.config import settings
from app.core.embeddings import EmbeddingClient
from app.core.llm_client import LLMClient
from app.domains import AssistantSpec
from app.observability.tracing import span
from app.core.vector_store import InMemoryVectorStore
from app.prompts import load as load_prompt

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent.parent / "data"  # app/agents/ → 项目根

# prompt 模板在 prompts/（T3-1）：{role_intro} 由 AssistantSpec 注入，{id_field} 是该领域的条目 ID 字段名。
# 规则部分（不编造/指出差异/不硬答/JSON 输出）是机制层的，属于引擎不随领域变


def _default_spec() -> AssistantSpec:
    """SSRM 值班助手：默认领域（保持旧行为向后兼容）"""
    return AssistantSpec(kb_file=_DATA_DIR / "ssrm" / "kb_tickets.jsonl", threshold=0.55)


class RagAssistant:
    def __init__(self, llm: LLMClient, spec: Optional[AssistantSpec] = None):
        self.llm = llm
        self.spec = spec or _default_spec()
        # prompt 外置（T3-1）：构造即加载，配置错误在启动阶段暴露
        self._answer_prompt = load_prompt("rag_answer")
        self._rerank_prompt = load_prompt("rag_rerank")
        self.store = InMemoryVectorStore()
        self.docs: list[dict[str, Any]] = []
        self._doc_grams: Optional[list[frozenset[str]]] = None
        self._ready = False
        self._embed_client: Optional[EmbeddingClient] = None

    # ── 知识库预热 ──
    async def warmup(self):
        tickets = [json.loads(line) for line in self.spec.kb_file.open(encoding="utf-8") if line.strip()]
        self.docs = tickets

        # 向量缓存按知识库文件名隔离，避免多领域互相覆盖；
        # 有效性凭据是知识库**内容** hash（T4-5）——旧实现只比条数，
        # KB 内容改了条数没变时会带着脏缓存上线
        kb_hash = hashlib.sha256(self.spec.kb_file.read_bytes()).hexdigest()[:16]
        cache_file = _DATA_DIR / f"kb_vectors_{self.spec.kb_file.stem}.json"
        cache = InMemoryVectorStore()
        if cache.load(cache_file) and cache.meta.get("kb_hash") == kb_hash:
            self.store = cache
            self._ready = True
            logger.info("知识库从缓存加载", extra={"event": "rag_cache_hit", "stage": "rag", "count": len(tickets), "outcome": "ok"})
            return
        if cache_file.exists():
            logger.info("向量缓存失效（知识库内容变更），重建索引",
                        extra={"event": "rag_cache_stale", "stage": "rag",
                               "cached_hash": cache.meta.get("kb_hash"), "kb_hash": kb_hash, "outcome": "rebuild"})

        provider = self._pick_provider()
        if provider is None:
            logger.warning("无可用 embedding Provider，知识库以关键词+LLM重排模式运行",
                           extra={"event": "rag_mode_degraded", "stage": "rag", "reason": "no_embedding_provider", "outcome": "degraded"})
            self._ready = True
            return

        self._embed_client = EmbeddingClient(provider)
        texts = [f"{t['title']} {t['symptom']}" for t in tickets]
        try:
            vectors = await self._embed_client.embed(texts)
        except Exception as e:
            # 例：智谱 embedding-2 需要资源包（余额不足包装成 429），免费档不可用
            self.degrade_to_keyword(str(e))
            return
        self.store.add(tickets, vectors)
        self.store.save(cache_file, meta={"kb_hash": kb_hash, "count": len(tickets)})
        self._ready = True
        logger.info("知识库预热完成", extra={"event": "rag_warmed", "stage": "rag", "count": len(tickets), "outcome": "ok"})

    def degrade_to_keyword(self, reason: str) -> None:
        """放弃向量检索，退回关键词召回 + LLM 重排。

        两条路径都会走到这里：embedding 调用失败（没额度/没 key），
        以及启动预热超时（服务不能为了建索引一直不接请求）。
        """
        self._embed_client = None
        self._ready = True
        logger.warning("embedding 不可用，降级为关键词召回 + LLM 重排模式",
                       extra={"event": "rag_mode_degraded", "stage": "rag",
                              "reason": reason[:160], "outcome": "degraded"})

    def _pick_provider(self):
        """取容灾链中第一个可用 Provider 做 embedding（模型名换成 embedding 模型）"""
        for p in self.llm.chain:
            return type("P", (), {"base_url": p.base_url, "api_key": p.api_key,
                                  "embedding_model": settings.embedding_model})()
        return None

    @property
    def ready(self) -> bool:
        return self._ready

    async def aclose(self):
        """退出时释放 embedding 连接池（由 main.lifespan 调用）"""
        if self._embed_client is not None:
            await self._embed_client.aclose()

    @property
    def mode(self) -> str:
        if not self._ready:
            return "not_ready"
        return "vector" if len(self.store) > 0 else "keyword+rerank"

    # ── 检索 ──
    async def _retrieve(self, question: str, top_k: int, budget: Optional[Budget] = None) -> list[dict[str, Any]]:
        if len(self.store) > 0 and self._embed_client:
            try:
                with span("rag.embed", mode="hybrid") as s:
                    qvec = (await self._embed_client.embed([question]))[0]
                    s["attrs"]["dim"] = len(qvec)
                hits = self.store.search(qvec, top_k=top_k)           # score = 余弦相似度
                kw_hits = self._keyword_search(question, top_k=top_k)  # score = 字符覆盖率
                # T4-4 混合检索：RRF（k=60）融合两路排名。向量通道对同义改写强、
                # 对专名/错误码弱；字符通道相反——两路信号互补，融合排序比单路稳。
                fused = self._rrf_fuse([hits, kw_hits], top_k=top_k)
                kw_score_by_id = {h[self.spec.id_field]: h["score"] for h in kw_hits}
                for d in fused:
                    d["match_type"] = "hybrid"
                    d["keyword_score"] = kw_score_by_id.get(d[self.spec.id_field], 0.0)
                s["attrs"]["fused"] = len(fused)
                return fused
            except Exception as e:
                logger.warning("向量检索失败，降级关键词检索",
                               extra={"event": "rag_retrieve_degraded", "stage": "rag", "reason": str(e)[:160], "outcome": "degraded"})
        # 关键词召回（宽召回 8 条）→ LLM 重排序取 Top-N
        with span("rag.keyword", mode="keyword") as s:
            candidates = self._keyword_search(question, top_k=8)
            s["attrs"]["candidates"] = len(candidates)
        return await self._llm_rerank(question, candidates, top_k, budget)

    def _rrf_fuse(self, rankings: list[list[dict[str, Any]]], top_k: int, k: int = 60) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion：score(d) = Σ 1/(k + rank_i(d))，k 取论文经验值 60。

        只融合排名不融合原始分——余弦分与字符覆盖率量纲不同，直接加权是伪精度；
        RRF 只消费排名，天然回避量纲问题。返回条目保留各自通道打来的原始字段
        （以 vector 副本为基底），rrf 分仅用于排序。
        """
        id_f = self.spec.id_field
        fused: dict[Any, dict[str, Any]] = {}
        for ranking in rankings:
            for rank, doc in enumerate(ranking, 1):
                key = doc.get(id_f) or id(doc)
                entry = fused.setdefault(key, {"doc": doc, "rrf": 0.0, "best_rank": len(ranking) + 1})
                entry["rrf"] += 1.0 / (k + rank)
                entry["best_rank"] = min(entry["best_rank"], rank)
        ordered = sorted(fused.values(), key=lambda e: (-e["rrf"], e["best_rank"]))
        return [dict(e["doc"], rrf=round(e["rrf"], 5)) for e in ordered[:top_k]]

    async def _llm_rerank(
        self, question: str, candidates: list[dict[str, Any]], top_k: int,
        budget: Optional[Budget] = None,
    ) -> list[dict[str, Any]]:
        """小规模知识库的取舍：关键词宽召回 + LLM 重排序，替代 embedding 向量检索

        分数语义（T4-6 修复）：重排只改变顺序，**不改写分数**——每个候选保留
        `_keyword_search` 的真实字符重叠率（bigram 覆盖率）。此前的
        `score = 1.0 - rank*0.1` 让第一名恒为 1.0、永远不触发采信阈值，
        等于 keyword+rerank 模式下门禁形同虚设；真实分数下阈值判断恢复意义
        （与 T5-01 按 keyword 分数分布定标的 0.4/0.3/0.2 同一口径）。
        分数与排名不再单调是诚实代价：LLM 认为相关的候选可能字符分不高。
        """
        if not self.llm.is_configured or len(candidates) <= top_k:
            return candidates[:top_k]
        try:
            cands = [{"cand_id": i, "title": c["title"], "symptom": c["symptom"][:80]}
                     for i, c in enumerate(candidates)]
            prompt = (f"用户提问：{question}\n\n候选历史案例：\n"
                      + json.dumps(cands, ensure_ascii=False)
                      + f'\n\n请选出与提问最相关的至多 {top_k} 条，按相关性降序。'
                        '只输出 JSON：{"order": [cand_id, ...]}')
            with span("rag.rerank", candidates=len(candidates)) as s:
                s["attrs"]["prompt_version"] = self._rerank_prompt.label
                result = await self.llm.chat_json(
                    self._rerank_prompt.text, prompt, budget=budget)
                order = [int(i) for i in result.get("order", []) if isinstance(i, int) and 0 <= i < len(candidates)]
                s["attrs"]["ordered"] = len(order)
            if not order:
                return candidates[:top_k]
            # 只重排，不改分数：score 保留真实字符重叠率（T4-6）
            return [candidates[i] for i in order[:top_k]]
        except Exception as e:
            logger.warning("LLM 重排失败，使用关键词序",
                           extra={"event": "rag_rerank_failed", "stage": "rag", "reason": str(e)[:160], "outcome": "degraded"})
            return candidates[:top_k]

    @staticmethod
    def _admitted(h: dict[str, Any], threshold: float) -> bool:
        """采信门禁（T4-4）：任一可信信号过阈值即采纳。

        keyword 模式：score 即字符覆盖率，与定标口径一致；
        hybrid 模式：score 是余弦、keyword_score 是字符覆盖率——后者已被
        三域扫描定标（0.4/0.3/0.2），作为第二采信信号参与判断，防止
        RRF 把「向量弱、关键词强」的命中排到前面后反被余弦阈值误杀。
        已知简化：两信号共用同一个数值阈值，按通道分开定标是后续项。
        """
        return (h.get("score") or 0) >= threshold or (h.get("keyword_score") or 0) >= threshold

    def _grams(self, text: str) -> frozenset[str]:
        """字符 bigram 集合。比 unigram 强在「近重复区分」：
        KB 扩到 150 条后（同簇变体共享几乎全部单字），unigram 集合重叠率趋于相同，
        无法区分「 XF-885 」和「 XF-883 」；bigram 保留序列信息（-88/885/883），
        区分度实测恢复（见 rag_eval 150 条规模评测）。"""
        t = "".join(text.split())
        return frozenset(t[i:i + 2] for i in range(len(t) - 1))

    def _keyword_search(self, question: str, top_k: int) -> list[dict[str, Any]]:
        # 文档 bigram 集合按知识库内容惰性缓存（docs 不变时只建一次）
        if self._doc_grams is None or len(self._doc_grams) != len(self.docs):
            self._doc_grams = [self._grams(f"{t['title']}{t['symptom']}") for t in self.docs]
        qg = self._grams(question)
        scored = []
        for t, dg in zip(self.docs, self._doc_grams):
            score = len(qg & dg) / max(len(qg), 1)
            scored.append({**t, "score": round(score, 4), "match_type": "keyword"})
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]

    # ── 提问答复 ──
    async def assist(self, question: str, budget: Optional[Budget] = None) -> dict[str, Any]:
        if not self._ready:
            return {"error": "知识库未就绪"}
        spec = self.spec

        with span("rag.retrieve", top_k=settings.rag_top_k, mode=self.mode) as s:
            hits = await self._retrieve(question, top_k=settings.rag_top_k, budget=budget)
            hits = [h for h in hits if self._admitted(h, spec.threshold)]
            s["attrs"]["hits"] = len(hits)

        if not hits:
            return {
                "answer": "知识库中没有找到相似的历史案例，建议升级人工处理，并把该 case 归因后沉淀进知识库。",
                "confidence": 0.0,
                "sources": [],
                "mode": self.mode,
            }
        id_f, class_f = spec.id_field, spec.class_field
        # LLM 总结（不可用则直接返回原始 case）
        if self.llm.is_configured:
            try:
                context = json.dumps(
                    [{id_f: h[id_f],
                      "现象": f"{h['title']}：{h['symptom']}",
                      "类别": h[class_f],
                      "处置过程": h["resolution"]} for h in hits],
                    ensure_ascii=False)
                system_prompt = self._answer_prompt.render(
                    role_intro=spec.role_intro or "智能助手", id_field=id_f)
                with span("rag.answer", hits=len(hits)) as s:
                    result = await self.llm.chat_json(
                        system_prompt, f"提问：{question}\n\n相似历史案例：{context}", budget=budget)
                    s["attrs"]["provider"] = result.get("_provider")
                result["sources"] = [{id_f: h[id_f], "title": h["title"],
                                      class_f: h[class_f],
                                      "score": h["score"]} for h in hits]
                result["mode"] = self.mode
                result["prompt_version"] = self._answer_prompt.label
                result["llm_provider"] = result.pop("_provider", "unknown")
                return result
            except Exception as e:
                logger.warning("LLM 总结失败，降级返回原始 case",
                               extra={"event": "rag_answer_degraded", "stage": "rag", "reason": str(e)[:160], "outcome": "degraded"})

        return {
            "answer": "LLM 暂不可用，以下是检索到的最相似历史案例，请人工参考：",
            "cases": [{id_f: h[id_f], "title": h["title"], "symptom": h["symptom"],
                       "resolution": h["resolution"]} for h in hits],
            "confidence": max(h["score"] for h in hits),
            "sources": [{id_f: h[id_f], "score": h["score"]} for h in hits],
            "mode": self.mode,
        }
