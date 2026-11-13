# T4-05 · 向量缓存按知识库内容 hash 失效

> 改动：`app/core/vector_store.py`（save/load 增加 meta）、`app/agents/rag_assistant.py`（warmup 缓存判断）
> 对应任务清单：T4-5

## 1. 问题

warmup 的缓存有效性判断只有一条：

```python
if cache.load(cache_file) and len(cache) == len(tickets):
```

只比**条数**。两个真实风险：

1. KB 内容改了条数没变（改错别字、换处置话术、同条数重排）→ 命中脏缓存，
   向量对应的是旧文本，检索结果与知识库静默错位；
2. 换 embedding 模型（`embedding_model` 配置变更）→ 维度或语义空间变了，缓存照用。

本质：**缓存缺一个与内容绑定的有效性凭据**。

## 2. 改动

| 位置 | 改动 |
|---|---|
| `InMemoryVectorStore.save` | 增加 `meta` 参数，随向量一起持久化 |
| `InMemoryVectorStore.load` | 回读 `meta`（旧格式文件得到 `{}`） |
| `rag_assistant.warmup` | `kb_hash = sha256(KB 文件字节)[:16]`；缓存命中条件从「条数相同」改为「hash 相同」；save 时写入 `{kb_hash, count}` |
| 日志 | 失效重建时打 `rag_cache_stale`（带新旧 hash），命中打 `rag_cache_hit`（原样保留） |

旧格式缓存文件（无 meta）→ `meta={}` → hash 比对失败 → 自动重建一次，无需迁移脚本。

## 3. 验收

| # | 验收项 | 结果 |
|---|---|---|
| 1 | save/load meta 往返 | ✅ `{'kb_hash': 'abc123', 'count': 1}` 完整回读 |
| 2 | 旧格式缓存（无 meta） | ✅ 视为失效，触发重建 |
| 3 | hash 稳定性 | ✅ 同一 KB 文件 hash 可复现（`cb14bc087423eb19` / 150 条） |

## 4. 边界

1. hash 是**内容级**的：改一个字节就重建。多实例部署时各实例独立判断，同一镜像内
   KB 不变则 hash 一致，不会出现实例间缓存漂移；
2. 换 embedding 模型的失效不在本改动内（meta 里没有模型名）——如需，把
   `settings.embedding_model` 加进 meta 一行即可，留作显式未做。
