# T4-04 · 向量 + 关键词 RRF 混合检索（附：评测非确定性修复）

> 改动：`app/agents/rag_assistant.py`（`_retrieve` / `_rrf_fuse` / `_admitted`）、`app/evaluation/rag_eval.py`（簇并集修复）
> 对应任务清单：T4-4；偏离原方案处：不用 rank_bm25，沿用项目已有的 bigram 字符打分做第二路

## 1. 动机与取舍

原清单方案是「向量分 + BM25 分，RRF 融合」。落地时改用**已有的字符 bigram 打分**
做第二路，理由：

1. BM25 需要引 `rank_bm25` 依赖并维护分词/索引；bigram 打分已在 `_keyword_search`
   里实现且被三域定标过（T5-01），零新依赖；
2. T4-6 刚把 keyword 分数修成真实可解释口径——两路信号语义都清楚；
3. 信号互补性成立：向量通道对同义改写强、对专名/错误码弱；字符通道正相反
   （`XF-885` vs `XF-883` 靠 bigram 区分，T5-01 实证）。

## 2. 设计：RRF 只管排序，阈值语义不变

```
vector 路径（原：纯余弦 Top-K）
  → 余弦 Top-K ∪ 字符 Top-K → RRF(k=60) 融合排序
  → score 保留余弦（阈值主口径），keyword_score 回填第二信号
  → 采信门禁：任一信号过阈值即采纳（_admitted）
```

关键取舍三条：

| 决策点 | 选择 | 为什么 |
|---|---|---|
| 融合什么 | 只融合**排名**，不融合原始分 | 余弦与字符覆盖率量纲不同，直接加权是伪精度；RRF 天然回避量纲问题 |
| score 字段 | 保留余弦 | 三域阈值的主口径不被破坏；`keyword_score` 单列 |
| 采信门禁 | `score ≥ t or keyword_score ≥ t` | RRF 可能把「向量弱、关键词强」的命中排到前面——若只看余弦会被误杀，混合就失去意义。两阈值同值复用是已知简化，按通道分开定标是后续项 |
| keyword 路径 | 不变 | 降级通道维持 keyword+LLM 重排；RRF 只在向量可用时生效 |

k=60 取 Cormack et al. (2009) 经验值；并列时按 best_rank 决胜，保证确定性。

## 3. 顺带修复：评测非确定性（本任务最重要的意外收获）

T4-6 后跑回归发现困难集簇级 Recall@1 在两次运行间漂移（129/130 vs 128/130）。
排查：

```python
q_cluster = next(iter({cluster.get(t, t) for t in relevant}))   # rag_eval.py:85（旧）
```

HRQ-000 / HRQ-002（批量回调丢失形态）的 relevant **跨两个簇**，
`next(iter(set))` 每次随机取一个簇当「相关簇」→ 结果随 `PYTHONHASHSEED` 漂移。

修复：相关簇 = 所有相关条目所属簇的**并集**（「命中任一相关现象即算对」的本意）。
三个不同 hash seed 验证，数字完全一致。

> 教训：**评测脚本自己要先过确定性关**。一个随机的评测器会让所有「提升 1 个点」
> 的结论失去意义——这次是 RRF 逼着重跑回归才暴露的。

## 4. 验收

| # | 验收项 | 结果 |
|---|---|---|
| 1 | RRF 排序正确性（两路排名互补的构造样例） | ✅ 双路都见的候选浮到前面，并列按 best_rank 决胜 |
| 2 | 采信门禁 | ✅ 余弦 0.35 + 关键词 0.52 → 放行；双弱 → 拦下；keyword 模式旧行为不变 |
| 3 | hybrid 标注与回填 | ✅ match_type=hybrid、keyword_score、rrf 字段齐全 |
| 4 | keyword 通道回归 | ✅ 三域数字与基线逐位一致（RRF 不在降级路径上） |
| 5 | 评测确定性 | ✅ 三个 PYTHONHASHSEED 下 ssrm 常规 99.3/97.3、困难 99.2/97.7 完全一致 |
| 6 | import 冒烟 | ✅ app.main 可导入 |

## 5. 边界

1. **RRF 的线上收益无法离线量化**：离线评测走 keyword 通道（无 embedding 额度），
   hybrid 排序的真实提升需要 vector 通道评测集（向量化 150 条 KB + 真实 embedding）——
   已知未做，不是不想测；
2. 两信号共用同一数值阈值，严格做法是 cosine 分布单独扫描定标；
3. union 门禁略增放行量（keyword 信号单独即可放行），代价由二阶段 LLM 总结的
   grounding 约束兜底。
