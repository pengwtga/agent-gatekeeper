"""RAG 值班助手 — 历史工单检索 + LLM 处置建议

流程（面试重点）：
1. 知识库预热：30 条历史 oncall 工单 → 智谱 embedding-2 向量化 → 内存向量库（JSONL 持久化）
2. 值班提问：问题向量化 → 余弦相似度 Top-3 检索 → 相似 case 作为上下文 → LLM 总结处置建议
3. 双级降级：embedding 不可用 → 关键词检索兜底；LLM 不可用 → 直接返回检索到的原始 case
4. 采信阈值：相似度 < 0.55 的检索结果标记"低置信"，提示值班人工判断
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional

from app.budget import Budget
from app.config import settings
from app.embeddings import EmbeddingClient
from app.llm_client import LLMClient
from app.vector_store import InMemoryVectorStore

logger = logging.getLogger(__name__)

KB_FILE = Path(__file__).parent.parent / "data" / "kb_tickets.jsonl"
CACHE_FILE = Path(__file__).parent.parent / "data" / "kb_vectors.json"

SYSTEM_PROMPT = """你是 SSRM（供应商管理系统）的值班助手。值班同事遇到卡单/同步失败问题时会来问你。
你会收到：①值班同事的提问；②知识库中检索到的相似历史工单（含现象、根因、处置过程）。
任务：基于历史工单给出处置建议。规则：
1. 优先参考历史工单的实际处置方式，不要凭空编造系统里不存在的操作
2. 如果历史 case 与当前问题不完全一致，明确指出差异
3. 如果检索结果都不相关，直接说不确定，建议升级人工——不要硬答
4. 只输出 JSON：{"answer": "处置建议（150字内，分步骤）", "confidence": 0.0, "based_on": [引用的 ticket_id]}"""


class RagAssistant:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.store = InMemoryVectorStore()
        self.docs: list[dict[str, Any]] = []
        self._ready = False
        self._embed_client: Optional[EmbeddingClient] = None

    # ── 知识库预热 ──
    async def warmup(self):
        tickets = [json.loads(line) for line in KB_FILE.open(encoding="utf-8") if line.strip()]
        self.docs = tickets

        cache = InMemoryVectorStore()
        if cache.load(CACHE_FILE) and len(cache) == len(tickets):
            self.store = cache
            self._ready = True
            logger.info(f"[RAG] 知识库从缓存加载: {len(tickets)} 条")
            return

        provider = self._pick_provider()
        if provider is None:
            logger.warning("[RAG] 无可用 embedding Provider，知识库以关键词+LLM重排模式运行")
            self._ready = True
            return

        self._embed_client = EmbeddingClient(provider)
        texts = [f"{t['title']} {t['symptom']}" for t in tickets]
        try:
            vectors = await self._embed_client.embed(texts)
        except Exception as e:
            # 例：智谱 embedding-2 需要资源包（余额不足包装成 429），免费档不可用
            logger.warning(f"[RAG] embedding 不可用（{e}），降级为关键词召回 + LLM 重排模式")
            self._embed_client = None
            self._ready = True
            return
        self.store.add(tickets, vectors)
        self.store.save(CACHE_FILE)
        self._ready = True
        logger.info(f"[RAG] 知识库预热完成: {len(tickets)} 条已向量化")

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
                qvec = (await self._embed_client.embed([question]))[0]
                return self.store.search(qvec, top_k=top_k)
            except Exception as e:
                logger.warning(f"[RAG] 向量检索失败，降级关键词检索: {e}")
        # 关键词召回（宽召回 8 条）→ LLM 重排序取 Top-N
        candidates = self._keyword_search(question, top_k=8)
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
            prompt = (f"值班提问：{question}\n\n候选历史工单：\n"
                      + json.dumps(cands, ensure_ascii=False)
                      + f'\n\n请选出与提问最相关的至多 {top_k} 条，按相关性降序。'
                        '只输出 JSON：{"order": [cand_id, ...]}')
            result = await self.llm.chat_json(
                "你是检索重排序器。只依据问题与工单的语义相关性排序，不要编造。",
                prompt, budget=budget)
            order = [int(i) for i in result.get("order", []) if isinstance(i, int) and 0 <= i < len(candidates)]
            if not order:
                return candidates[:top_k]
            picked = [candidates[i] for i in order[:top_k]]
            # 相似度分数用排名近似（1.0 递减），供采信阈值判断
            for rank, c in enumerate(picked):
                c["score"] = round(1.0 - rank * 0.1, 2)
            return picked
        except Exception as e:
            logger.warning(f"[RAG] LLM 重排失败，使用关键词序: {e}")
            return candidates[:top_k]

    def _keyword_search(self, question: str, top_k: int) -> list[dict[str, Any]]:
        q = question.lower()
        scored = []
        for t in self.docs:
            text = f"{t['title']}{t['symptom']}".lower()
            score = sum(1 for kw in set(q) if kw in text) / max(len(set(q)), 1)
            scored.append({**t, "score": round(score, 4)})
        scored.sort(key=lambda d: d["score"], reverse=True)
        return scored[:top_k]

    # ── 值班问答 ──
    async def assist(self, question: str, budget: Optional[Budget] = None) -> dict[str, Any]:
        if not self._ready:
            return {"error": "知识库未就绪"}

        hits = await self._retrieve(question, top_k=settings.rag_top_k, budget=budget)
        hits = [h for h in hits if h.get("score", 0) >= settings.rag_min_score]

        if not hits:
            return {
                "answer": "知识库中没有找到相似的历史工单，建议升级人工排查，并把根因沉淀进知识库。",
                "confidence": 0.0,
                "sources": [],
                "mode": self.mode,
            }        # LLM 总结（不可用则直接返回原始 case）
        if self.llm.is_configured:
            try:
                context = json.dumps(
                    [{"ticket_id": h["ticket_id"],
                      "现象": f"{h['title']}：{h['symptom']}",
                      "根因类别": h["root_cause_class"],
                      "处置过程": h["resolution"]} for h in hits],
                    ensure_ascii=False)
                result = await self.llm.chat_json(
                    SYSTEM_PROMPT, f"值班提问：{question}\n\n相似历史工单：{context}", budget=budget)
                result["sources"] = [{"ticket_id": h["ticket_id"], "title": h["title"],
                                      "root_cause_class": h["root_cause_class"],
                                      "score": h["score"]} for h in hits]
                result["mode"] = self.mode
                result["llm_provider"] = result.pop("_provider", "unknown")
                return result
            except Exception as e:
                logger.warning(f"[RAG] LLM 总结失败，降级返回原始 case: {e}")

        return {
            "answer": "LLM 暂不可用，以下是检索到的最相似历史工单，请人工参考：",
            "cases": [{"ticket_id": h["ticket_id"], "title": h["title"], "symptom": h["symptom"],
                       "resolution": h["resolution"]} for h in hits],
            "confidence": max(h["score"] for h in hits),
            "sources": [{"ticket_id": h["ticket_id"], "score": h["score"]} for h in hits],
            "mode": self.mode,
        }
