# T5-0 · 泛化三件事：Domain Pack 剥离 + 通用工具层 + 第二领域验证

日期：2026-11-13 ｜ 状态：✅ 已完成

## 动机

项目被评价为"偏运维侧"。但真正值钱的机制（容灾链、预算、门禁、证据回溯、评测回归、span）
全部领域无关——偏运维的是**知识的位置**：5 类定义、few-shot、规则基线硬编码在
`agents/classifier.py` 里。本次改造把知识剥成插件，并补上通用 Agent 的标志能力（工具调用）。

## 1 · Domain Pack：引擎与领域知识分离

```
app/domains/
├── __init__.py      # DomainPack 契约：role_intro / classes / few_shot / rule_baseline
├── ssrm/            # 供应商运维领域（taxonomy.py / rules.py / pack.py）
└── csa/             # 电商客服领域（第二实例）
```

- `agents/classifier.py` 重写为**领域无关引擎**：通道编排、输出校验、证据门禁、置信度门禁；
  知识全部来自注入的 pack；
- `__init__.py` 的 docstring 就是 pack 契约，实现不许偏离；
- 面试话术升级："分类引擎是领域无关的，接新领域 = 写一个 pack + 一套评测集，引擎零改动。"

## 2 · 通用工具调用层（为什么不 用 Function Call / LangChain）

`app/core/tools.py`（~80 行，零依赖）+ `app/core/builtin_tools.py`（3 个内置只读工具）：

- **不用 Function Call**：项目全线走 JSON mode（glm-4-flash 不支持 response_format，
  且 JSON mode 跨 Provider 行为一致、可评测）；混用两套协议 = 双倍评测面；
- **不用 LangChain AgentExecutor**：它把"选了什么、为什么、参数是什么"埋进抽象——
  与本项目"每步可观测可门禁"的立身之本冲突；
- **三道关**（与分类器同一门禁哲学）：白名单（LLM 编造的工具名到此为止）→
  schema 校验（必填/类型/声明外参数）→ 置信度门禁（<0.7 不执行）；
  `read_only=False` 的工具一律拒绝自动执行（副作用工具接权限系统之前宁可空手而归）；
- 端点：`GET /tools`（注册表清单）、`POST /tools/ask`（选工具→校验→执行）。

实测（glm-4-flash）：
- "知识库里有多少条工单" → 正确选 `kb_stats` 并执行，conf 1.0；
- "查 duplicate_submit 类型的 5 条历史事件" → 正确选 `query_incidents`，参数类型正确；
- "把生产服务器重启一下" → `tool: null`，不选不执行（超出注册表职责，转人工）。

## 3 · 第二领域实例（迁移的最硬证明）

`domains/csa/`：电商客服工单分类（退款纠纷 / 物流延迟 / 售前咨询 / 发票开具 / 账号安全），
知识面与 SSRM 完全正交——没有系统拓扑、没有错误码，判断依据是用户原话。

| 通道 | CSA 客服域 | SSRM 运维域 |
|---|---|---|
| 规则基线 | 30/30（100%） | 30/30（100%） |
| LLM 容灾链 | **29/30（96.7%）** | 100% |
| 平均延迟 | 2803 ms | ~3.7s |

规则迭代过程本身就值得讲：初版 80%（6 个混淆），修正后发现一个比关键字更强的判据——
**没有订单（order_status=none）就不可能是售后类**。领域知识的演化全部发生在 pack 目录内，
引擎零改动。

评测入口：`python -m app.evaluation.eval 30 --domain csa [--rule]`

## 已知边界

- 工具层是单步选择（选一个工具执行一次），多步规划（工具链）未做——那是 T5 LangGraph 的议题；
- CSA 困难样本混在全量集里（口语化），没有独立 hard 集；
- `POST /tools/ask` 是独立演示端点，尚未接入 /assist 的对话流。
