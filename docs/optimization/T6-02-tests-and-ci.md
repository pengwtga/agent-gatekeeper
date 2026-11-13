# T6-02 · 测试套件 + CI（工程卫生收尾）

日期：2026-11-13 ｜ 状态：✅ 已完成

## 测试（44 个，纯函数优先）

| 文件 | 覆盖 |
|---|---|
| `test_llm_json.py` | `_extract_json` 四形态：纯 JSON / 围栏 / 前后噪音 / 无 JSON（含半截 JSON 不许当结果） |
| `test_rules.py` | ssrm 五类 + unknown 兜底；credit 六分支（含 dti 0.6~0.7 灰区 unknown）——规则是评测 baseline，它错了所有对比数字跟着错 |
| `test_evidence.py` | 两判据：refs 全对/数字字符串等价/字段缺失/值编造/嵌套路径/空返回 None 触发回退/二次编码数组/子串引用；字面匹配诚实区间（0.4~0.75，T0-03 标定）与纯编造 |
| `test_budget.py` | deadline 制语义：remaining 只随墙钟衰减、slice 是被动检查不消耗、monotonic |
| `test_pipeline_escalation.py` | 状态机四路径（auto_dispose/escalate/human_review/步数熔断）+ 升级队列闭环（落库/续跑/决议不可覆盖） |

测试确定性原则：**不烧 token**——用 `is_configured=False` 的假 LLM 强制规则通道；
LLM 通道的行为由评测集（eval.py）覆盖，测试只锁机制。

## CI（GitHub Actions）

`ruff check` + `pytest -q`，单版本（3.12）矩阵。ruff 规则取向写在 `ruff.toml`：
只开「会出 bug 的」E4/E7/E9/F + import 排序，不开全量风格集；
BLE001（blind except）显式忽略——容灾链路刻意宽捕获后记日志降级，是设计决定。

## T6-4 表述对齐核验结果

| 表述 | 核验 |
|---|---|
| 「遇 400 自动降级宽松 JSON 提取」 | ✅ 成立：`_call` 400 → `_JSONModeUnsupported` → 无 json_mode 重试（不耗重试次数）→ `_extract_json` 三级救回 |
| 「证据链可审计 / 逐条可人工复核」 | ✅ 成立：T0-03/T3-5 已落地，`/classify` 返回 refs 明细（ok/actual/reason） |
| 「评测集回归」 | ✅ 成立：常规 100 条 / 困难 25 条 / 回归 20 条数字与文件一致；`/eval` token 限制已在端点 docstring 与 README 注明 |

三处全部与代码对齐，无需改措辞——它们在 T0/T3 批次已经按「能修就去修」的原则修掉了。
