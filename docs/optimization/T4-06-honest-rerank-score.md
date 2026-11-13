# T4-06 · 修「降级模式伪造 score」：重排不再改写分数

> 改动：`app/agents/rag_assistant.py`（`_llm_rerank` / `_keyword_search` / `_retrieve` / 模块 docstring）、`app/evaluation/rag_eval.py`（docstring）
> 对应任务清单：T4-6（原清单推荐方案 A，实际落地取「真实字符分 + match_type」的改良形式）

## 1. 问题

T0-1 之后 keyword+rerank 成为三域线上主通道，但 `_llm_rerank` 里有一段遗留：

```python
# 相似度分数用排名近似（1.0 递减），供采信阈值判断
for rank, c in enumerate(picked):
    c["score"] = round(1.0 - rank * 0.1, 2)
```

后果是一条**门禁失效 bug**：重排后第一名 score 恒为 1.0，`assist()` 的
`hits = [h for h in hits if h.get("score") >= spec.threshold]` 在该模式下**永远放行**——
哪怕提问与知识库完全无关。T3-00 时把它文档化为「阈值门禁只在分数可解释通道生效」的
已知边界，但正确解法不是绕开它，而是让分数可解释。

## 2. 判断

原清单给了两个方案：A = score 置 None + 按通道走不同常量；B = 保留真实字符重叠率。
实际取 B 的加强版，理由：

1. **阈值的口径本来就是它**。T5-01 的三域定标（0.4 / 0.3 / 0.2）扫的就是 keyword
   bigram 字符分数的分布——伪造分数反而是唯一让阈值失配的东西；
2. score=None 会让 `sources` 里的分数失去意义，前端引用条没有可显示的依据；
3. 「分数与排名不再单调」（LLM 认为相关的候选字符分可能不高）是**诚实代价**，
   恰好可视化了两通道的分歧——这正是评测想看的东西。

## 3. 改动

| 位置 | 改动 |
|---|---|
| `_llm_rerank` | 删掉伪造段，只重排不改分；docstring 说明分数语义与动机 |
| `_keyword_search` | 每条结果带 `match_type: "keyword"` |
| `_retrieve` vector 路径 | 每条命中带 `match_type: "vector"` |
| `rag_eval` docstring | 「已知边界」更新：重排分数与召回同口径，阈值扫描对两通道都有意义 |

阈值判断一行未改——修的是分数的**真实性**，不是阈值的**数值**。

## 4. 验收

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 三域 rag_eval 离线回归（keyword 通道） | ✅ 与基线逐位一致（ssrm 簇级 99.3% / strict 97.3%、csa 100%、credit 87.5%）——排序未变，只去掉了分数改写 |
| 2 | 无关提问（「附近有什么火锅店」） | ✅ top1 真实分数 0.0 < 阈值 0.4 → 过滤为空 → 返回「没找到相似案例」confidence 0.0（旧代码重排后恒 1.0 高置信） |
| 3 | 模拟 LLM 重排（FakeLLM 指定 order） | ✅ 重排后每个候选分数与重排前一致（断言通过），只换序不改分 |
| 4 | match_type 标注 | ✅ keyword / vector 两通道来源可区分 |

## 5. 边界（被追问时主动说）

1. vector 通道分数是余弦相似度，与 keyword 字符分数**不可直接比较**；
   spec.threshold 一个字段承载两种语义是既有限制——当前离线定标基于 keyword 分布，
   vector 通道若成为主通道，需按 cosine 分布重扫（做法同 T5-01）。
2. LLM 重排可能把字符分低的候选排到前面，top1_score 因此可能低于第二名——
   这是两通道分歧的可见化，不是 bug；若业务要求单调性，应让 LLM 输出置信度而非复用分数。
