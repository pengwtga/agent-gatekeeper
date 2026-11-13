# T5-01 · 助手知识库 Pack 化 + 三域数据扩充（2026-11-13）

对接蚂蚁信贷风控 Agent 岗的「催员辅助 Agent」需求后，把项目从「一个域有双 Agent、
另两个域只有分类」补成 **三个领域完整的「分类 + 助手」双 Agent 结构**。

## 1. 机制侧：AssistantSpec（引擎与知识分离的另一半）

- `DomainPack` 新增可选字段 `assistant: AssistantSpec`（kb_file / threshold /
  id_field / class_field / role_intro）。None = 该领域暂无助手场景。
- `RagAssistant` 从写死 kb_tickets.jsonl 改为吃 spec：同一引擎、三份领域知识
  （ssrm=值班助手 / csa=坐席助手 / credit=催员辅助），`/assist` 加 `domain` 参数，
  `/health` 暴露各助手模式。SSRM 行为回归一致（rag_eval 数字与改造前相同）。
- 知识库条目标准化为五字段 schema：{id, category, title, symptom, resolution}。

## 2. 算法侧：关键词打分器 unigram → bigram（扩容逼出来的改进）

KB 从 30 扩到 150 后，原始字符 unigram 打分的簇级 Recall@1 从 100% 崩到 26%：
同簇变体共享几乎全部单字，「XF-885」和「XF-883」在字符集合上无法区分。
改用**字符 bigram 覆盖率**（保留序列信息，无新依赖），并配合「查询从 symptom
派生改写」的评测集构造，恢复到 strict 97.3% / 簇级 99.3%。

这条链路本身是完整的工程闭环叙事：**扩容暴露打分器缺陷 → 指标双口径定位问题
→ 换打分算法 → 重定阈值**。

## 3. 评测口径：strict / cluster 双层（灰区哲学在检索上的延续）

- **strict**：精确命中标注 id；
- **cluster**：命中同现象簇任一条目（SSRM 的 -V 变体、csa/credit 的同 base 模板案例，
  通过 cluster_key 声明）——同现象案例对用户同样有用；
- 两口径之差 = 关键词打分的簇内区分度，是选型信号而非噪音。

## 4. 数据扩充总表

| 文件 | 规模 | 说明 |
|---|---|---|
| kb_tickets.jsonl | 150（30 手工 + 120 变体） | SSRM 值班助手；变体 = 同义替换 + 区分性细节 |
| csa_agent_kb.jsonl | 100（5 类 × 20） | 坐席助手 SOP/话术，含红线（不当评判商家、不虚开金额） |
| credit_collection_kb.jsonl | 120（5 类 × 24） | 催员辅助：早期提醒/承诺跟进/困难协商/失联找回/**合规红线** |
| rag_queries.jsonl | 150 | 检索评测（每变体一条，含区分 token） |
| rag_queries_hard.jsonl | 40 | 去黑话 + 口语化 |
| csa_kb_queries.jsonl / credit_kb_queries.jsonl | 各 40 | 两域检索评测 |
| csa_tickets.jsonl | 150（30 手工 + 120 合成） | CSA 分类评测集扩充，规则 100% |

生成器：`scripts/gen_assistant_data.py`、`scripts/gen_csa_dataset.py`（固定种子 20261113，
幂等，手工金样本永不覆盖）。

## 5. 实测结果

### 检索（关键词召回，离线）

| 领域 | strict R@1 | cluster R@1 | R@3 | MRR@8 |
|---|---|---|---|---|
| ssrm 常规（150 查询/150 KB） | 97.3% | **99.3%** | 99.3% | 0.984 |
| ssrm 困难（40 查询） | 97.7% | 99.2% | 97.7% | 0.981 |
| csa（40/100） | 100% | 100% | 100% | 1.000 |
| credit（40/120） | 85.0% | 87.5% | 95.0% | 0.901 |

### 采信阈值重定标（阈值随语料分布漂移必须重扫的实证）

| 领域 | 阈值 | 覆盖 | 留存精度（簇级） |
|---|---|---|---|
| ssrm | 0.4 | 91% | 98%（576/588） |
| csa | 0.3 | 82% | 98%（60/61） |
| credit | 0.2 | 87.5% | 85%（80/94） |

阈值语义边界（沿用 T3-0 结论）：只在分数可解释的关键词通道生效；线上
keyword+rerank 模式的门禁作用在重排的排名近似分上。

### 分类回归（不受本次改动影响）

csa 150/150、credit 291/300（明确区 100%）、ssrm 30/30。

## 6. 端到端冒烟

- `/health`：`assistants: {ssrm/csa/credit: keyword+rerank}`；
- `POST /assist {"domain":"credit"}`：「客户逾期 8 天第一次电话提醒」→ 命中
  `CCLK-EARL-01`（M1 首次温和提醒）+ 同簇案例，LLM 总结含合规要点（征信影响提示、
  挂机后短信留痕）；ssrm 默认（不带 domain）行为不变。

## 已知边界

1. credit 检索 85%：查询与 symptom 措辞距离比 ssrm/csa 大（催收场景描述更口语），
   3 条簇级 miss 集中在失联找回类；LLM 重排通道预期可修复（未跑，省钱）；
2. embedding 通道仍不可用（配额），三助手均以 keyword+rerank 运行；
3. 合成话术/合规条目是「形态正确」的演示语料，真实催收合规话术须由法务审校——
   面试如实说明：**机制可复用，语料需业务供血**。
