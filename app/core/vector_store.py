"""轻量向量存储 — NumPy 余弦相似度

设计取舍（面试可讲）：
- 知识库规模 < 1000 条时，NumPy 暴力余弦检索毫秒级完成，无需引入 Faiss/Chroma
- 接口与向量库对齐（add / search），后续规模上来可无缝替换为持久化方案
- 向量与文档一起持久化为 JSONL，服务重启不丢
"""

import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np


class InMemoryVectorStore:
    def __init__(self):
        self._docs: list[dict[str, Any]] = []
        self._matrix: Optional[np.ndarray] = None

    def __len__(self) -> int:
        return len(self._docs)

    def add(self, docs: list[dict[str, Any]], vectors: list[list[float]]):
        self._docs.extend(docs)
        new = np.array(vectors, dtype=np.float32)
        self._matrix = new if self._matrix is None else np.vstack([self._matrix, new])

    def search(self, query_vector: list[float], top_k: int = 3) -> list[dict[str, Any]]:
        if self._matrix is None or len(self._docs) == 0:
            return []
        q = np.array(query_vector, dtype=np.float32)
        # 余弦相似度（向量已归一化时等价于点积，这里显式归一化）
        norms = np.linalg.norm(self._matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        qn = np.linalg.norm(q)
        if qn == 0:
            return []
        sims = (self._matrix @ q) / (norms.flatten() * qn)
        top_idx = np.argsort(sims)[::-1][:top_k]
        return [
            {**self._docs[int(i)], "score": round(float(sims[i]), 4)}
            for i in top_idx
        ]

    def save(self, path: Path):
        with path.open("w", encoding="utf-8") as f:
            json.dump({"docs": self._docs, "vectors": self._matrix.tolist()}, f)

    def load(self, path: Path) -> bool:
        if not path.exists():
            return False
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        self._docs = data["docs"]
        self._matrix = np.array(data["vectors"], dtype=np.float32)
        return True
