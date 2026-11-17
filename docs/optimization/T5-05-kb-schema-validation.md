# T5-05 知识库入库校验：从「人肉约定」到启动期断言

> 一句话：KB 从来只有 `json.loads` 一层，类名写错、同组动作写岔都能静默加载；
> 现在补两层校验——**O(n) 结构层在启动期炸，O(n²) 语义层在 CI 跑**。
> 上线当天它抓到 160 行真问题（ssrm 40 行措辞不一致、credit 120 行类名不在任何枚举里）。

## 1. 问题

`RagAssistant.warmup()` 此前是：

```python
tickets = [json.loads(line) for line in self.spec.kb_file.open(encoding="utf-8") if line.strip()]
```

没有字段完整性、没有类名枚举、没有跨行一致性。实测（改前）：造一条 `root_cause_class="ea_timeout"`
（正确值 `ea_sync_timeout`）的条目 → 加载照过、检索照样能召回、答案里就显示一个分类字典里不存在的类名；
缺 `resolution` 也不报错，只在生成答案时降级成一句空话。

后果不是「崩」，而是**错得很安静**：

- 答案引用一个 5 类体系里不存在的类目，下游按类目路由的处置逻辑会落空；
- KB 里 `category` 与分类体系的对应关系只存在于作者脑子里，改一个字母没有任何反馈；
- 三份 KB、370 条、两个人写过（手写 30 条 + 脚本生成 120 条变体），靠「我记得检查过」不成立。

## 2. 判断

**能不能让代码替人记住？** 能，但要分清两种检查的成本量级：

| 层 | 复杂度 | 跑在哪 | 查什么 | 成本 |
|---|---|---|---|---|
| 结构校验 | O(n) | 启动期 + CLI + CI | 解析、必填、类型、空值、类名枚举、ID 唯一、分组一致 | 370 条 < 5ms |
| 语义体检 | O(n²) | 仅 CLI / CI | 簇内自召回：这条能不能被同簇条目召回 | 370 条 0.16s，10k 条约分钟级 |

把 O(n²) 塞进启动期是典型的「用力过猛」：它在 150 条时无感，涨到 10k 条就把启动拖成分钟级，
而它要防的错误（新条目措辞跑偏）恰恰是在**提交那一刻**产生的，不是每次重启都会新出现。

**为什么是断言而不是警告**：坏数据上线比不启动更贵。KB 是检索答案的唯一事实来源，
类目错了，答案就错了，而用户看不出错——这类错误必须挡在进程之外。
（与本项目既有取向一致：`register()` 在启动期校验参数类型、prompts 在启动期校验 active 指针。）

## 3. 方案取舍

**① 不用 pydantic 反序列化。**
五字段 schema 是三个领域的**最小共同面**，字段名各自映射（`ticket_id`/`case_id`、
`root_cause_class`/`category`）。用 pydantic 会逼所有领域统一字段名，等于用校验工具反向
改造领域模型；而且 pydantic 天然只做单行校验，「同组条目某字段必须一致」这种跨行约束
仍要自己写。直接用 90 行纯函数，零新依赖、报错带行号和字段名。

**② 约束由领域声明，校验器只执行。**
`AssistantSpec` 新增三项：`classes`（类名枚举）、`required_fields`、`consistency`
（`ConsistencyRule(group_by, same)`）。校验器只认识「分组字段 + 约束字段」，
不认识业务含义——ssrm 声明「同 `root_cause_class` 的 `standard_action` 必须一致」，
csa/credit 声明「同 `cluster_key` 的 `category` 必须一致」，一份代码覆盖两种形态。

`classes` 默认由 `DomainPack.__post_init__` 从 `pack.classes` 注入：类名权威只有一处，
领域作者不必在两个地方抄同一份枚举（抄漏一个值正是「人肉对齐」的来源）。

**③ credit 的例外：两套词表就声明两套。**
credit 的助手知识库按**催收场景**组织（`early_reminder`/`promise_pay`/…），
而分类器分的是**进件风险**（`fraud_suspect`/`multi_debt`/…）。
校验器一上线就报了 120 条 `E_CLASS_UNKNOWN`——不是数据错了，是「这套取值合法吗」
从来没被声明过。处理方式是给它一个 `COLLECTION_STAGES` 枚举（含每类定义）并在
`AssistantSpec` 里显式传 `classes=`：**「同源」是要求，但源不一定是分类词表**；
是两套就明说，不让引擎去猜。

**④ 40 行 `standard_action` 归一到分类体系。**
ssrm 手写 30 条母本时，同一个根因被写成了 2~3 种动作措辞（`主动拉取工单状态补齐` /
`主动拉取工单状态同步` / `更新回调配置`），变体继承母本，于是 40 行不一致。
字段本身没有被引擎消费（答案上下文用的是 `resolution`），但它是「一类一动作」的展示口径，
放着就是给下一个人埋坑。修法不是逐条改文案，而是把短标签提到
`taxonomy.STANDARD_ACTIONS`（与 `ROOT_CAUSE_CLASSES` 同文件、加一条键集合相等的单测防漂移），
`gen_kb.py` 从它取，数据一次性归一。**同一语义只留一处定义。**

**⑤ 召回体检用 bigram，不调 embedding。**
`--recall` 拿每条自己的 `title+symptom` 当 query，看同簇其它条目能否进 Top-3。
用字符 bigram 覆盖率而不是向量检索，理由是确定性和成本：CI 里不能有随机性、不能有额度依赖，
而这一层要查的是「用词是否与同簇同源」——词汇面的事，bigram 就够。
代价明确：**测不了同义改写**（「审批回调没到」vs「回调通知未达」算不同词），
所以它只作为「新条目措辞跑偏」的兜底，不替代人读一遍。

**⑥ 只报告，不自动修复。**
拿 `difflib` 最接近的枚举值自动改？看着聪明，实际会把「作者理解错了类目」
静默改写成一个看起来合法的值。校验器给提示（`最接近 'ea_sync_timeout'`），
改不改由人决定。

## 4. 改动

| 文件 | 内容 |
|---|---|
| `app/kb/validate.py`（新） | 校验器 + CLI：`_parse` / `_check_structure` / `_check_consistency` / `_check_recall` / `load_kb` / `validate_rows` |
| `app/core/text.py`（新） | `bigrams()`：检索与体检共用的字符二元组口径 |
| `app/domains/__init__.py` | `ConsistencyRule`；`AssistantSpec.classes/required_fields/consistency`；`DomainPack.__post_init__` 注入枚举 |
| `app/domains/ssrm/taxonomy.py` | `STANDARD_ACTIONS`（5 类短标签，KB 动作的唯一来源） |
| `app/domains/ssrm/__init__.py` | 声明 `ConsistencyRule("root_cause_class", ("standard_action",))` |
| `app/domains/csa|credit/__init__.py` | 声明 `ConsistencyRule("cluster_key", ("category",))` |
| `app/domains/credit/taxonomy.py` | `COLLECTION_STAGES`（催收场景词表，与进件风险词表并列） |
| `app/agents/rag_assistant.py` | `warmup()` 改走 `load_kb()`；`_grams` 委托 `core.text.bigrams` |
| `data/ssrm/kb_tickets.jsonl` | 40 行 `standard_action` 归一到 `STANDARD_ACTIONS` |
| `scripts/gen_kb.py` | 动作从 `STANDARD_ACTIONS` 取，不再用手写元组里的字面量 |
| `tests/test_kb_validate.py`（新） | 19 例：枚举/必填/唯一/一致性/召回/启动期抛错/装配注入 |
| `Makefile` / `ci.yml` | `make kb-check`、CI 增加 `python -m app.kb.validate --recall` |

CLI 用法：

```bash
python -m app.kb.validate                 # 全部已注册领域，结构层
python -m app.kb.validate --recall        # 追加 O(n²) 簇内自召回（CI 用）
python -m app.kb.validate --domain csa data/csa/csa_agent_kb.jsonl   # 单文件
python -m app.kb.validate --json          # 给 CI 解析
```

错误码固定，CI 按码过滤比匹配文案稳：`E_PARSE` / `E_MISSING_FIELD` / `E_BAD_TYPE` /
`E_EMPTY_FIELD` / `E_CLASS_UNKNOWN` / `E_DUP_ID` / `E_INCONSISTENT` / `E_NOT_RECALLABLE`，
警告级：`W_SHORT_FIELD` / `W_SINGLETON_CLASS` / `W_NO_CLASS_ENUM`。

## 5. 验收

```bash
$ make kb-check
[PASS] ssrm       data/ssrm/kb_tickets.jsonl                     150 条 · 0 error · 0 warning
[PASS] csa        data/csa/csa_agent_kb.jsonl                    100 条 · 0 error · 0 warning
[PASS] credit     data/credit/credit_collection_kb.jsonl         120 条 · 0 error · 0 warning

合计 3 个文件 / 370 条 / 0 个错误          # 0.16s，不调 LLM
```

`pytest -q` → **108 passed**（原 88 + 新增 19 + 1 条检索公共件回归）；`ruff check` 全过。

**启动期拦截实测**（往 csa KB 追加一条 `category="refund_disupt"` 后启动服务）：

```
ERROR app.kb.validate  event=kb_invalid  msg=知识库校验失败，拒绝加载  outcome=fail
ERROR uvicorn.error    Application startup failed. Exiting.
uvicorn exit=3

KBValidationError: csa_agent_kb.jsonl:101  E_CLASS_UNKNOWN  category  'refund_disupt'
不在 5 类枚举内；最接近 'refund_dispute'；
csa_agent_kb.jsonl:101  E_INCONSISTENT  cluster_key='csa:refund_dispute:B0' 的该字段
应为「refund_dispute」（见第 1 行），实际「refund_disupt」
```

一条坏数据同时被两条规则抓住（枚举 + 簇内一致），且报出「第 101 行、哪个字段、最接近的合法值」。
正常 KB 启动冒烟：`/health` 200，三域助手 `keyword+rerank` 就绪。

**改前的真实数字**（校验器首次运行，未修数据时）：`160 个错误`——ssrm 40（`E_INCONSISTENT`）、
credit 120（`E_CLASS_UNKNOWN`）。这就是「150 条我核对过是干净的」这句话的含金量：
类名和字段确实干净，**跨行一致性从来没人查过**。

## 6. 边界

1. **不判语义质量**。`resolution` 写得对不对、话术是否合规，校验器管不了；它只管结构、
   枚举、跨行一致和词汇面召回。语义质量靠评测集（T3-00）与人读。
2. **warning 不拦启动**。标题过短、某类只有 1 条只是提醒——这类「可能有问题」不该阻断服务，
   否则 warning 会被当成噪声关掉。
3. **多域共进程，一坏全挂**。三个域在同一个 FastAPI 进程里，任一 KB 不合格整个服务起不来。
   这是「宁可不上线也不带病上线」的代价；分域部署或多实例时自然缓解。
4. **不负责「是否被改过」**。版本与审计归 git（`git log -p data/ssrm/kb_tickets.jsonl`），
   要更强就内容 hash + 审计表；校验器只回答「改完之后合不合格」。
5. **召回体检是词汇面**。同义改写检测不到；它拦的是「另起一套说法」，不是「语义有偏差」。
6. **不做自动修复**，理由见 §3⑥。

## 7. 面试讲法

> 知识库是我这个项目最大的软肋：检索、答案、评测全都建立在它上面，但它的正确性一直只有
> `json.loads` 一层保护。我做过实验，把类名从 `ea_sync_timeout` 改成 `ea_timeout`，
> 加载照过、检索照召回，答案里就输出一个分类字典里不存在的类目——**错得很安静**。
>
> 补法我分了两层，因为两种检查的成本差两个数量级：结构校验是 O(n)，解析/必填/枚举/
> 唯一/同组一致，放启动期，不合格直接拒绝启动——坏数据上线比不启动更贵；簇内自召回是 O(n²)，
> 要拿每条去和全库比，10k 条会把启动拖成分钟级，所以只放 CI，在提交那一刻跑。
>
> 约束是领域声明的，校验器只执行：ssrm 声明「同根因的动作必须一致」，csa/credit 声明
> 「同簇的类目必须一致」，一份 90 行纯函数覆盖两种形态，加簇维度不用改校验器。
>
> 上线第一天它报了 160 行：ssrm 40 行同一类被写成三种动作措辞，credit 120 行类目
> 根本不在任何枚举里——因为 credit 的助手按催收场景组织，和分类器的进件风险是两套词表，
> 而这件事以前从没被写下来。前者归一到一个 `STANDARD_ACTIONS` 常量，后者补一个
> `COLLECTION_STAGES` 枚举，两处都是「同一语义只留一处定义」。
>
> 我刻意没做自动修复：拿最接近的枚举值顶上去，会把「人理解错了」悄悄改成一个看起来合法的值。
> 提示给到「第 101 行、最接近 refund_dispute」，改不改是人的决定。
