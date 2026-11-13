"""RAG 助手引擎 — 领域无关：历史案例检索 + LLM 处置建议

机制与知识分离（面试重点）：
引擎只负责机制——宽召回 + LLM 重排 / 向量检索、采信阈值门禁、grounding 总结、
双级降级；「知识库里放什么、阈值多少、助手的人设边界」由领域的 AssistantSpec 声明
（ssrm=值班助手 / csa=坐席助手 / credit=催员辅助），同一引擎服务三个领域。

流程（以 SSRM 值班助手为例）：
1. 知识库预热：历史工单 → 智谱 embedding-2 向量化 → 内存向量库（JSONL 持久化）
2. 提问：问题向量化 → 余弦相似度 Top-K 检索 → 相似 case 作为上下文 → LLM 总结处置建议
3. 双级降级：embedding 不可用 → 关键词检索 + LLM 重排兜底；LLM 不可用 → 直接返回原始 case
4. 采信阈值：分数 < 阈值（spec.threshold）的检索结果标记"低置信"，提示人工判断
"""

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

        # 向量缓存按知识库文件名隔离，避免多领域互相覆盖
        cache_file = _DATA_DIR / f"kb_vectors_{self.spec.kb_file.stem}.json"
        cache = InMemoryVectorStore()
        if cache.load(cache_file) and len(cache) == len(tickets):
            self.store = cache
            self._ready = True
            logger.info("知识库从缓存加载", extra={"event": "rag_cache_hit", "stage": "rag", "count": len(tickets), "outcome": "ok"})
            return

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
        self.store.save(cache_file)
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
                with span("rag.embed", mode="vector") as s:
                    qvec = (await self._embed_client.embed([question]))[0]
                    s["attrs"]["dim"] = len(qvec)
                return self.store.search(qvec, top_k=top_k)
            except Exception as e:
                logger.warning("向量检索失败，降级关键词检索",
                               extra={"event": "rag_retrieve_degraded", "stage": "rag", "reason": str(e)[:160], "outcome": "degraded"})
        # 关键词召回（宽召回 8 条）→ LLM 重排序取 Top-N
        with span("rag.keyword", mode="keyword") as s:
            candidates = self._keyword_search(question, top_k=8)
            s["attrs"]["candidates"] = len(candidates)
        return await self._llm_rerank(question, candidates, top_k, budget)

    async def _llm_rerank(
        self, question: str, candidates: list[dict[str, Any]], top_k: int,
        budget: Optional[Budget] = None,
    ) -> list[dict[str, Any]]:
        """小规模知识库的取舍：关键词宽召回 + LLM 重排序，替代 embedding 向量检索"""
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
            picked = [candidates[i] for i in order[:top_k]]
            # 相似度分数用排名近似（1.0 递减），供采信阈值判断
            for rank, c in enumerate(picked):
                c["score"] = round(1.0 - rank * 0.1, 2)
            return picked
        except Exception as e:
            logger.warning("LLM 重排失败，使用关键词序",
                           extra={"event": "rag_rerank_failed", "stage": "rag", "reason": str(e)[:160], "outcome": "degraded"})
            return candidates[:top_k]

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
            scored.append({**t, "score": round(score, 4)})
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]

    # ── 提问答复 ──
    async def assist(self, question: str, budget: Optional[Budget] = None) -> dict[str, Any]:
        if not self._ready:
            return {"error": "知识库未就绪"}
        spec = self.spec

        with span("rag.retrieve", top_k=settings.rag_top_k, mode=self.mode) as s:
            hits = await self._retrieve(question, top_k=settings.rag_top_k, budget=budget)
            hits = [h for h in hits if h.get("score", 0) >= spec.threshold]
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
