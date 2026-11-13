"""Embedding 客户端 — 沿用 Provider 容灾链

复用 LLMClient 的 Provider 链（key/base_url），调用 OpenAI 兼容的 /embeddings 接口。
智谱的 embedding-2 返回 1024 维向量。

工程细节：免费档 embedding 接口有速率限制（实测 30 条单批会触发 429），
因此做了 ①分批（每批 ≤10 条）②429 指数退避重试——这是接免费 LLM API 的必修课。
"""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

BATCH_SIZE = 10


def _is_quota_exhausted(resp: httpx.Response) -> bool:
    """429 里区分「限流」与「没额度」：后者重试没有意义。

    智谱余额不足返回 HTTP 429 + {"error": {"code": "1113", "message": "余额不足或无可用资源包…"}}，
    与真正的限流共用状态码。只有 1113 这类才是不可恢复的。
    """
    try:
        err = resp.json().get("error") or {}
    except Exception:
        return False
    return str(err.get("code")) == "1113" or "余额" in str(err.get("message", ""))


class EmbeddingClient:
    def __init__(self, provider):
        self.provider = provider
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return self.provider is not None

    @property
    def client(self) -> httpx.AsyncClient:
        """共享连接池：warmup 批量向量化 + 每次提问的问题向量化共用，省掉重复 TLS 握手"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=2.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
            )
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """分批向量化 + 429 退避重试。全部失败抛异常，由上层降级（关键词检索）。"""
        results: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start:start + BATCH_SIZE]
            results.extend(await self._embed_batch_with_retry(batch))
            if start + BATCH_SIZE < len(texts):
                await asyncio.sleep(1)  # 批间限速
        return results

    async def _embed_batch_with_retry(self, batch: list[str]) -> list[list[float]]:
        backoff = 2
        last_err: Exception | None = None
        for attempt in range(4):  # 最多 4 次：2s/4s/8s/16s
            try:
                return await self._call(batch)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and not _is_quota_exhausted(e.response):
                    logger.warning("embedding 429 限流，退避重试",
                                   extra={"event": "embedding_rate_limited", "stage": "embedding",
                                          "attempt": attempt + 1, "reason": f"backoff={backoff}s", "outcome": "retry"})
                    last_err = e
                    await asyncio.sleep(backoff)
                    backoff *= 2
                else:
                    # 余额不足/无资源包也被智谱包装成 429，但重试一万次也不会成功，
                    # 只会把启动卡住 30s × 批次数。识别出来直接抛，让上层降级。
                    raise
        raise last_err

    async def _call(self, batch: list[str]) -> list[list[float]]:
        resp = await self.client.post(
            f"{self.provider.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.provider.api_key}", "Content-Type": "application/json"},
            json={"model": self.provider.embedding_model, "input": batch},
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]
