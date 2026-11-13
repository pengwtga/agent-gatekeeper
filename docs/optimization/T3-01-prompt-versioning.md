# T3-01 Prompt 版本化 + 回归集（T3-1 / T3-2 / T3-3 / T3-4）

> 新增：`app/prompts.py`、`prompts/`（index.json + 3 个 v1 模板）、`data/ssrm/regression.jsonl`、`Makefile`
> 改动：`app/agents/classifier.py`、`app/agents/rag_assistant.py`、`app/main.py`、`app/config.py`、`app/evaluation/eval.py`

## 1. 问题

prompt 一直是代码里的 f-string：分类的 system prompt 在 `classifier._build_system()`，
助手的人设与规则在 `rag_assistant.SYSTEM_PROMPT_TMPL`，重排提示词干脆内联在 `_llm_rerank` 的调用里。

后果有三层：

1. **不可回溯**：线上某天分类变差，无法回答「当时用的是哪版 prompt」——因为 prompt 没有身份。
   日志里只有 provider / model / token，没有 prompt。
2. **不可对比**：想改一句「证据不足时如实给低分」，改完只能凭体感说"好像好点了"。
   T3-0 已经把**检索**从体感变成了数字，但**生成侧**还是体感。
3. **改动不可审**：prompt 混在 Python 代码里，一次「只改语气」的提交会淹没在 diff 里，
   review 时看不到"行为变了"。

## 2. 判断

prompt 不是配置，是**行为的一部分**——改一个字，所有样本的输出都可能变。
所以它需要和代码一样的待遇：**有版本、有指针、有校验、可追溯**。

但版本化本身不产生价值。**版本化 + 回归集**才产生价值：
版本让你能回到上一版，回归集让你能回答"要不要回"。
两者顺序不能反——先做 T3-1 再做 T3-2，否则 v2 写出来也没有尺子量。

## 3. 方案取舍

| 决策点 | 选择 | 为什么 |
|---|---|---|
| 存储形态 | 文件 + `index.json`（active 指针） | 文件即真相：diff 可审、git 可回滚、无需额外服务。不引 LangSmith/promptflow——这个体量用不上，且外部平台会让"当前线上是哪版"变成要联网才能回答的问题 |
| 模板引擎 | `str.format` | prompt 里只有平铺变量（`{role_intro}` / `{classes}` / `{id_field}`），Jinja2 的循环/条件在这里没有用武之地，少一个依赖少一层黑盒 |
| 校验时机 | **服务启动（lifespan）**，不是 import | 单测 import 模块不该被 `prompts/` 绊倒；但服务必须 fail fast——带着错的 prompt 跑起来比不上线更糟 |
| 声明校验 | 版本表声明 `vars`，与文本实际占位符比对 | 改了模板忘同步声明 → 启动失败，而不是运行到某个分支才 `KeyError`。等价于"prompt 的 schema 检查" |
| 行尾换行 | loader 显式去掉文件末尾一个 `\n` | md 文件天然以换行结尾，旧 f-string 没有。不去掉，"外置"就不是纯搬运，回归集的分差会混入噪声 |
| 分数存哪 | `prompts/scores.json`，不写回 `index.json` | 索引是**声明**，分数是**运行时观测**。混在一起，每次切版本都会夹带一堆数字 diff，审不了 |
| 热更新 | 不做 | prompt 能被外部文件悄然改变 = 线上行为不可审计。切版本走重启，宁可麻烦 |

## 4. 改动

### prompts/

```
prompts/
├─ index.json            # 唯一入口：{name: {active, versions: {v1: {file, vars, note, created}}}}
├─ root_cause.v1.md      # 分类 system prompt（{role_intro} {domain} {classes} {n_classes}）
├─ rag_answer.v1.md      # 助手答复 prompt（{role_intro} {id_field}）
└─ rag_rerank.v1.md      # 检索重排 system prompt（无变量）
```

`index.json` 里三个 prompt 的 `active` 都是 `v1`，`note` 写明来源（"从 xxx 外置，文本逐字保留"）。

### app/prompts.py

```python
load("root_cause")                  # -> Prompt(name, version, text, vars)，带缓存
prompt.render(role_intro=..., ...)  # 缺变量抛 PromptError（不是 KeyError）
validate()                          # 启动校验：active 存在 / 文件存在 / 占位符声明一致
active_versions()                   # -> {"root_cause": "v1", ...}，给 /health
record_score("root_cause", "v1", "regression", 0.95)   # 分数归档
```

占位符用 `string.Formatter().parse()` 提取——`{{...}}` 这种转义字面量不会被误算成占位符，
所以 prompt 里的 JSON 示例（`{{"root_cause_class": ...}}`）不需要特殊处理。

### 接线

| 位置 | 改动 |
|---|---|
| `classifier.__init__` | `self._prompt = load("root_cause")` —— 构造即加载，启动阶段暴露配置错误 |
| `classifier._build_system` | 17 行 f-string → `self._prompt.render(role_intro, domain, classes, n_classes)` |
| `classifier.classify` | LLM 通道结果带 `prompt_version: "root_cause.v1"`（含灰区仲裁通道） |
| `rag_assistant.__init__` | `load("rag_answer")` + `load("rag_rerank")`，模块级 `SYSTEM_PROMPT_TMPL` 删除 |
| `rag_assistant.assist` | 结果带 `prompt_version`；重排 span 记 `prompt_version` 属性 |
| `main.lifespan` | 启动第一件事 `prompts.validate()`；启动日志的 reason 带三版本 |
| `main./health` | 新增 `prompts: {root_cause: v1, rag_answer: v1, rag_rerank: v1}` |
| `config.py` | `prompts_dir`（留空用仓库内 `prompts/`，可指向外部目录做 A/B） |

### data/ssrm/regression.jsonl（T3-2）

20 条**手工**样本，5 类根因各 4 条，字段与常规集同构（`incident` / `label` / `expected_action`），
多两个可选字段：`note`（这条在守什么）与 `evidence_probe`（证据门禁探针）。

四条一组的目的是把「一个类的正确判断」拆成不同难度：
**典型 / 边界 / 口语化 / 规模异常**。例如 `ea_sync_timeout`：
- REG-05 诚实详述（error_code + 504 + 重试 4 次）——T0-03 的证据探针 A
- REG-06 极简引用（"EA_TIMEOUT / 504"）——T0-03 的证据探针 J，与 A 构成**分数反转**
- REG-07 无错误码，只有"30 秒后连接被关闭"
- REG-08 重试耗尽 + 高敏感字段（银行账户变更）

### Makefile（T3-3）

```bash
make eval       # 回归集（--record）+ 困难集（--record）
make eval-rag   # 三域检索评测
make prompts    # prompts/ 自检
```

`eval.py` 新增 `--regression`（全量 20 条）与 `--record`（写 `prompts/scores.json`），
每次运行末尾打一行可直接对比的摘要：`[root_cause.v1] ssrm/回归集: 0.95`。

## 5. 验收

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 外置前后文本**逐字一致**（root_cause × ssrm/credit、rag_answer × 三域 intro） | ✅ 全部 True（含行尾换行归一化） |
| 2 | `make prompts` 自检 | ✅ `{'root_cause': 'v1', 'rag_answer': 'v1', 'rag_rerank': 'v1'}` |
| 3 | 三种坏配置启动即失败 | ✅ active 指向不存在版本 / 占位符声明不一致 / 文件缺失，均抛 `PromptError` 且信息可读 |
| 4 | `make eval` 回归集 | ✅ **19/20 = 95.0%**（LLM 通道 20/20） |
| 5 | `make eval` 困难集 | ✅ 25/25 = 100.0% |
| 6 | 证据探针 | ✅ REG-05 score 0.500 PASS / REG-06 score 0.684 PASS（均未触发门禁误伤） |
| 7 | 分数归档 | ✅ `prompts/scores.json`：root_cause.v1 → regression 0.95 / hard 1.00 |
| 8 | `/health` 暴露版本 | ✅ `prompts: {root_cause: v1, rag_answer: v1, rag_rerank: v1}` |
| 9 | `/classify` 返回版本 | ✅ `prompt_version: "root_cause.v1"`，evidence_score 0.684 |
| 10 | 三域分类回归未被破坏 | ✅ ssrm 20/20、csa 20/20、credit 正常（2 条灰区 unknown 属预期） |

### 唯一的 miss：REG-04

`同一批 3 张审批单（FT...371/372/373）全部通过，SSRM 均无任务`
→ 判成 `duplicate_submit`（正确是 `xflow_callback_lost`）。

LLM 被"多张单 / 批"的字面诱导到"重复"，而正确线索是"审批已通过但下游无任务"。
**这条不调样本**：改措辞把它改到 100%，等于让尺子迁就被测物。
保留它作为基线的一部分——它的价值是「批量回调丢失」这类形态的探测器，
将来 v2 若修好，分数会从 0.95 → 1.00，这本身就是一次可观测的改进。

## 6. 边界

1. **20 条样本量只能抓大退化**：能发现「改坏了某类整体判断」，发现不了「某类掉了 3 个点」。
   真正的统计口径要每类 50+ 且来自真实工单（见 `docs/data-catalog.md` 的扩充路径）。
2. **回归集与模型绑定**：REG-04 的 miss 是 glm-4-flash 的行为。换模型后基线要重跑一遍，
   不能拿别的模型的分数当"退步"。
3. **分数记录依赖本地跑**：`make eval` 目前是人工触发，没接 CI。
   接 CI 需要固定 LLM 额度与波动容忍（temperature=0 仍有非确定性）。
4. **切版本需重启**：不做热更新。多实例部署时要保证 `prompts/` 随镜像发布，不能各实例读不同文件。
5. **T3-5 未做**：证据从自由文本改为结构化引用（`evidence_refs`）还没动。
   这一步会改变所有样本的输出 schema，必须先有回归集才能证明没变差——现在地基有了。

## 7. 面试讲法

> 「prompt 版本化不是把字符串挪个文件，是三步：**声明、校验、留痕**。
> 声明是 index.json 的 active 指针，让『当前线上是哪版』有唯一答案，而且写在 git 里；
> 校验是启动时比对模板占位符和声明——改模板忘同步声明会直接启动失败，而不是跑一半 KeyError；
> 留痕是每条结果自带 prompt_version，评测分数按版本归档。
>
> 但只有版本化没用。我配了 20 条手工回归集，5 类各 4 条，按典型/边界/口语化/规模异常分布，
> 里面特意放了两条证据门禁的探针——就是 T0-03 里分数反转的那两条，
> 因为那正是 prompt 措辞一改就会漂的地方。
>
> 现在 `make eval` 一条命令，v1 的基线是回归 0.95、困难 1.00。我故意留了一个 miss 不修：
> 回归集是用来量模型的，不是用来迁就模型的。
>
> 另外我做了一个决定：prompt 不做热更新。能被外部文件悄然改掉的线上行为，是不可审计的。」

**配套数字**：`["root_cause.v1] 回归 0.95 / 困难 1.00`；三处 prompt 外置逐字一致（True）；三种坏配置启动即失败。
