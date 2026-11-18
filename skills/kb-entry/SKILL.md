---
name: kb-entry
description: 新增或修改一条知识库条目（data/*/kb_*.jsonl）时使用。走「选簇 → 填字段 → 机器校验 → 真链路验召回 → 提交」五步。只读检查、诊断已有知识库请直接用 make kb-check，不要触发本技能。
triggers: 新增知识库条目, 修改知识库条目, 加一条KB, 改一条KB, 新增一条工单库, kb 条目入库
---

# 知识库入库流程

一次线上问题 → 一条合格的知识库条目。目标是让「改数据的人不是原作者」时也不会写错。

## 何时用

- 要往 `data/ssrm/kb_tickets.jsonl`、`data/csa/csa_agent_kb.jsonl`、
  `data/credit/credit_collection_kb.jsonl` 里**加一条**，或**改一条**已有条目。
- 要新建一个领域的知识库（此时先看第 0 步）。

## 何时不用

| 你想做的事 | 用什么 |
|---|---|
| 只想看看现有 KB 合不合格 | `make kb-check` |
| 想知道某条数据有没有被人改过 | `git log -p <kb 文件>`，不是本技能 |
| 修检索代码 / 调阈值 | 不是数据问题，看 `docs/optimization/T4-*` |
| 改分类体系本身（加第 6 类） | 先改 `app/domains/<域>/taxonomy.py` + 重跑评测，再回来 |

## 动手之前

```bash
git diff --stat                     # 确认你改的是哪个文件、改了几条
git log -1 --format=%h -- <kb 文件>  # 上一次是谁动的
```

一次只加/改一条。批量导入走脚本（`scripts/gen_assistant_data.py` 这类），不要手改 20 行。

## 五步

**1. 判该不该入库**

能归到这个领域的某个**已有类目**吗？

- 能 → 继续。
- 不能，但确实是一类新问题 → 停。先回 `taxonomy.py` 加类、补 few-shot、重跑评测集，
  再回来写条目。**不要在 KB 里造一个分类体系没有的类目**（校验会拦，这是故意的）。
- 根本归不了类（只是「查一下」的咨询）→ 不是 KB 条目，转人工或补监控。

**2. 选簇，抄母本**

找到同现象的那条**母本**（无 `-V` 后缀、手写的那条），照它的口径写：

- ssrm：`root_cause_class` + `standard_action` 必须与母本一致（一类一动作，
  唯一来源是 `taxonomy.STANDARD_ACTIONS`）。
- csa / credit：`cluster_key` 的类目段必须与 `category` 一致（簇不跨类）。
- 新变体沿用母本的 `cluster_key`，不要自己编。

**3. 填内容**

| 字段 | 要求 | 反例 |
|---|---|---|
| `title` | 一句话说现象，10 字上下 | 「问题处理」 |
| `symptom` | 值班同学能对照的特征：报错码、状态、时长、涉及系统。**不写内部单号/人名** | 「用户反馈有问题」 |
| `resolution` | 可执行步骤：动作 + 判据（先查什么、看到什么算什么、不行怎么办） | 「加强监控」「后续优化」 |

`resolution` 是喂给模型当上下文的，写成「加强监控」等于教模型说空话。

**4. 跑机器校验（不过就停）**

```bash
make kb-check
# 等价于 python -m app.kb.validate --recall
```

- 有 `error` → 修到 0，**不要绕过**。退出码非 0 就是没通过。
- 只有 `warning`（字段偏短、某类只有 1 条）→ 可以提交，但要看一眼是不是真写成空壳了。
- 报错看三样东西：行号、字段、`最接近 'xxx'` 的提示。
  类名拼错时校验器会给出最接近的合法值，但**改不改由你判断**——不要盲从提示。

单文件快速迭代：

```bash
python -m app.kb.validate --domain ssrm data/ssrm/kb_tickets.jsonl --recall
```

**5. 真链路验一次召回**

机器校验只查词汇面（bigram 重合），查不了语义。服务在跑的话，用新条目的 `symptom`
原话问一次助手：

```bash
curl -s localhost:8001/assist -H 'content-type: application/json' \
  -d '{"question":"<新条目的 symptom 原话>"}' | jq '{sources, confidence, answer}'
```

三条都过才算合格：

1. `sources[0]` 落在同簇（或新条目自己就是 top1）；
2. `confidence` 不是 0（0 意味着被采信阈值拦了，说明这条的写法和库里其它条目不像）；
3. `answer` 里引用的类目名在 `taxonomy.py` 里存在。

## 提交前 checklist

- [ ] `make kb-check` 退出码 0
- [ ] 只加/改了一条，`git diff` 里没有意外的字段丢失或整行重排
- [ ] 新条目能被同簇条目召回（第 5 步）
- [ ] 如果动了 `taxonomy.py`：评测集重跑过，`prompts/scores.json` 有记录
- [ ] 如果加了新领域：`AssistantSpec` 的 `classes` / `consistency` 已声明
      （不声明就是悄悄少一道关，只会有一条 `W_NO_CLASS_ENUM`）

## 已知边界

校验器**不判断**内容对不对：话术是否合规、处置步骤是否真能执行、根因归类是否准确，
都要人读。它保证的是「结构、枚举、跨行一致、词汇面召回」这四件事。
