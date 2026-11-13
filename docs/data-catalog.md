# data 目录数据集目录（命名含义 · 作用 · 数据量评估）

> 生成日期：2026-11-13。数据按领域分目录，与 `app/domains/` 领域包一一对应。
> 所有数据由 `scripts/gen_*.py` 生成，固定随机种子（20261113）可复现；**手工金样本永不覆盖**（生成器只追加 ID 段不冲突的新行），重跑生成器是安全的。

## 一、命名规则速查

| ID 前缀 | 含义 | 出现文件 |
|---|---|---|
| `SIM-` | **SIM**ulated，SSRM 分类常规集合成事件 | ssrm/incidents.jsonl |
| `HARD-` | 困难集事件（改写/去错误码/口语化） | ssrm/incidents_hard.jsonl |
| `ONCALL-` / `ONCALL-xxx-V*` | 历史值班工单；`-V` 后缀是**同现象变体簇**（同一簇命中任一即算对） | ssrm/kb_tickets.jsonl |
| `RQ-` / `HRQ-` | 检索评测查询（R**Q**=常规、**H**ard RQ=困难） | ssrm/rag_queries*.jsonl |
| `CSA-` / `CSG-` | CSA 分类工单：**CSA**=手工金样本、**CSG**=参数化合成（**G**enerated） | csa/csa_tickets.jsonl |
| `CSAK-<类>-nn` | **CSA K**B 条目，中间段是类别码（REFU退款/LOGI物流/PRES售前/INVO发票/ACCO账号） | csa/csa_agent_kb.jsonl |
| `CQ-` / `CRQ-` | 检索查询：**C**SA **Q**uery / **C**redit R**Q**uery | csa/csa_kb_queries.jsonl、credit/credit_kb_queries.jsonl |
| `CRA-` / `CRG-` | 信贷进件：**CRA**=手工金样本（A 带灰区设计）、**CRG**=参数化合成 | credit/credit_applications.jsonl |
| `CCLK-<类>-nn` | **C**redit **C**ollection K**B** 条目，类别码（EARL早期提醒/PROM承诺跟进/HARD困难协商/LOST失联找回/COMP合规红线） | credit/credit_collection_kb.jsonl |

三种 schema：

1. **分类评测集**（incidents / csa_tickets / credit_applications）：`{incident: {...}, label, expected_action}` —— incident 模拟真实工单/进件字段，label 是标准答案，expected_action 描述正确处置。
2. **知识库条目**（kb_tickets / csa_agent_kb / credit_collection_kb）：统一五字段 `id/category/cluster_key/title/symptom/resolution`，与 `AssistantSpec` 契约对齐；resolution 写完整 SOP/话术，供 LLM grounding。
3. **检索查询集**（rag_queries* / csa_kb_queries / credit_kb_queries）：`{query_id, question, relevant: [应命中条目ID]}` —— 问题是对 symptom 的**改写**而非复制，避免"自己出题自己答"。

## 二、SSRM（运维值班）

| 文件 | 名称含义 | 规模 | 作用 |
|---|---|---|---|
| `ssrm/incidents.jsonl` | 合成事件样本 | 100 条（5 类 × 20：xflow_callback_lost / ea_sync_timeout / duplicate_submit / data_inconsistency / frontend_param_error） | SSRM 分类常规评测——回归防线，防改动退化、做通道对照 |
| `ssrm/incidents_hard.jsonl` | 困难事件集 | 25 条（改写表述、无 error_code、口语化） | 检验 LLM 相对规则基线的泛化差距（规则在此掉到 unknown，LLM 显出价值） |
| `ssrm/kb_tickets.jsonl` | 值班知识库 | 150 条 = 30 手工金样本 + 120 变体（`-V` 后缀同簇） | RAG 知识库；`/assist domain=ssrm` 的检索源；**0.4 采信阈值就是在这 150 条上扫描定标的** |
| `ssrm/rag_queries.jsonl` | 常规检索评测 | 150 条（30 手工 + 120 变体，每条 KB 条目一条查询） | 检索指标主口径（strict R@1 97.3% / 簇级 99.3%） |
| `ssrm/rag_queries_hard.jsonl` | 困难检索评测 | 130 条（10 手工 + 120 变体，去黑话 + 口语化） | 打关键词召回的语义鸿沟，支撑"要不要上 embedding"的论证 |
| `ssrm/regression.jsonl` | 回归集（T3-2） | 20 条**手工**样本（5 类 × 4，REG-01~20），含 `note` 与 2 条 `evidence_probe` | prompt 版本对比的尺子：`make eval` 跑它，v1 基线 19/20（REG-04 故意留 miss） |

## 三、CSA（电商客服）

| 文件 | 名称含义 | 规模 | 作用 |
|---|---|---|---|
| `csa/csa_tickets.jsonl` | 客服工单评测集 | 150 条 = 30 手工（CSA-）+ 120 合成（CSG-），5 类 × 30 | 第二领域分类评测，证明"同引擎换包"的泛化能力；规则基线 100% |
| `csa/csa_agent_kb.jsonl` | 坐席助手知识库 | 100 条（5 类 × 20：REFU/LOGI/PRES/INVO/ACCO） | `/assist domain=csa` 的检索源；话术红线内置（不当评判商家、不虚开金额） |
| `csa/csa_kb_queries.jsonl` | 检索评测查询 | 40 条 | CSA 助手检索指标（strict 100%） |

## 四、CREDIT（信贷风控）

| 文件 | 名称含义 | 规模 | 作用 |
|---|---|---|---|
| `credit/credit_applications.jsonl` | 信贷进件评测集 | 300 条 = 30 手工金样本（CRA-，含 9 条灰区带）+ 270 合成（CRG-），5 类 × 60 | 第三领域分类评测：**rule_first 规则先行 + 灰区 LLM 仲裁**漏斗的真实形态；明确区 291/291 |
| `credit/credit_collection_kb.jsonl` | 催员辅助知识库 | 120 条（5 类 × 24，含合规红线类） | `/assist domain=credit` 的检索源；合规红线（不威胁、不泄露债务信息） |
| `credit/credit_kb_queries.jsonl` | 检索评测查询 | 40 条 | CREDIT 助手检索指标（R@3 95%） |

## 五、数据量是否足够？

结论先行：**按"回归评测 + 面试演示 + 设计论证"的定位，够；按"统计置信 + 真实分布 + 生产就绪"的口径，不够，且这个边界要主动讲。**

### 够的部分

1. **回归防线的粒度够**：每类 20~60 条，足以捕捉"改动后某类掉点"的回归信号。评测集的价值不在大，在**稳定且和规则基线同构**——它就是给 CI 用的。
2. **设计决策的样本量够**：
   - 采信阈值 0.4/0.3/0.2 是在 150 条 KB + 150 条查询上扫描出来的，不是拍的；
   - benchmark 的规模曲线（万条关键词 p50 28.3ms）在 150 条真实 KB 上测工程成本、随机向量上测量算法成本，外推合理；
   - 灰区带（dti 0.60~0.70，9 条）刻意保留在 credit 评测集里，才能验证 rule_first 漏斗真的工作。
3. **检索评测覆盖对齐**：ssrm 常规/困难查询与 KB 条目 1:1（每条都有对应金标准），strict/簇级双口径指标可信。

### 不够的部分（诚实边界）

1. **常规集准确率偏乐观**：合成数据与规则基线同源（模板生成），规则在常规集上 100% 是"自己出的题自己会做"。常规集定位是回归，**区分能力靠困难集**——这在 README 已声明。
2. **统计置信不够**：每类 20~60 条，单类错 1 条 = 1.7~5pct 的波动，做不出 95% 置信区间级别的结论。对外汇报只能说"回归全绿"，不能说"准确率 97%±x"。
3. **类别覆盖是演示级**：SSRM 5 类、CSA 5 类、CREDIT 5 类，真实工单/进件的长尾远超 5 类（unknown 治理闭环就是为此设计的，但目前没有真实 unknown 数据喂进来）。
4. **检索评测部分抽样**：csa（KB 100 条只有 40 查询）、credit（KB 120 条只有 40 查询）只覆盖约 1/3 条目，指标是抽样口径。
5. **合成话术是"形态正确"**：话术红线、SOP 的措辞是演示语料，生产化必须由业务供血（真实脱敏工单、合规审核过的话术库）。

### 扩充路径

| 需求 | 动作 |
|---|---|
| 补统计置信 | 每类合成扩到 200+，或直接接入脱敏真实工单重建评测集 |
| 补真实分布 | 业务方导出历史工单/进件 → 重标注 → 生成器改为"真实底座 + 合成补稀疏类" |
| 补检索覆盖 | csa/credit 查询集扩到与 KB 条目 1:1（生成器加一轮派生即可） |
| 语义鸿沟更大 | 困难集加"换问法框架"（因果问、对比问）而非仅同义替换 |

相关文档：[T3-00 检索评测](optimization/T3-00-rag-retrieval-eval.md)（指标口径与阈值定标）、[T5-00 领域包](optimization/T5-00-domain-pack-and-tools.md)、[T5-01 助手 Pack 化](optimization/T5-01-assistant-kb-pack.md)（数据扩充批次记录）。
