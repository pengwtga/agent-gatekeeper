# T3-3 证据下限按「不同字段」计数

## 1. 问题

`min_evidence_refs=2` 的语义是「一条引用不足以支撑结论」，但实现只数**条目数**：

```python
if len(detail) < settings.min_evidence_refs:      # detail = 引用条目列表
```

而 `_value_matches` 对长文本允许「引用为原文子串」（截断是合理的，见 T3-2）。

两条规则叠在一起就漏了：**同一句 `error_message` 切成两段，就是 2 条都 `ok` 的引用**。
实测（`tests/test_evidence_gate.py::test_two_refs_on_same_field_are_blocked`）：

```
输入  error_message = "供应商提交了准入申请，过了好久一直没反应"
引用  [{field: error_message, value: "供应商提交了准入申请"},
      {field: error_message, value: "过了好久一直没反应"}]
结果  score=1.0  refs_count=2  →  门禁放行
```

也就是说，「至少两条证据」被一句半的话凑满了。下限想堵的是 T0-03 里那个
「少说少错」的分数反转（只引 1 条也是 1.000），结果被绕过的方式变成「一句话掰开说」。

一个次要发现：子串匹配有 `_MIN_SUBSTRING=8` 的门槛，所以这条洞只在**长文本**上成立。
长文本恰恰是真实告警的常见形态（`error_message` 通常是一句人话），所以不能当作理论问题。

## 2. 判断

「引用条数」这个量本身没有信息量，有意义的是**独立事实的个数**：

- 两条引用指向同一字段 → 一个事实，模型仍然只说了「这件事」一遍
- 两条引用指向不同字段 → 两个事实互相印证，这才是「支撑」

所以下限应该统计 `{d["field"] for d in detail if d["ok"]}` 的基数，而不是 `len(detail)`。

这里有个容易写错的地方：**只统计 `ok=True` 的字段**。用全部字段去重的话，
模型写三个字段名但值全对不上，也能凑够下限——那是把「字段名会不会背」当成了「证据够不够」。

## 3. 方案取舍

| 方案 | 取舍 |
|---|---|
| ✅ 按 `ok` 引用的不同字段数计下限 | 改 3 行；语义直接对上「两个独立事实」；老数据（`evidence_refs` 明细）不用迁移 |
| ❌ 要求每条引用的 `field` 不重复（去重后再算分） | 会改变 `refs_score` 的分母，等于动 T3-2 的标定 |
| ❌ 把 `_MIN_SUBSTRING` 调大到覆盖整句 | 会误杀合理的截断引用（模型引半个 `error_message` 是正常行为） |
| ❌ 直接删掉条数下限，只留分数阈值 | 重新打开 J 的洞（一条引用 1.000） |

## 4. 改动

`app/agents/classifier.py::_apply_evidence_gate`：

```python
valid_fields = {d["field"] for d in detail if d["ok"]}
result["evidence_refs_fields"] = len(valid_fields)      # 新增可观测字段
if len(valid_fields) < settings.min_evidence_refs:
    reason = f"仅 {len(valid_fields)} 个字段的有效引用（下限 {n} 个不同字段）"
```

- 结果里多一个 `evidence_refs_fields`，前端和 trace 都能看到「有几个独立字段」，
  而不只是「有几条引用」
- 拦截文案从「仅 N 条引用」改成「仅 N 个字段的有效引用」，把判据本身写进人话里
- `config.py` 的注释同步：下限按不同字段数算，并说明原因

## 5. 验收

`tests/test_evidence_gate.py`（4 例，桩 LLM，零 token）：

| 用例 | 期望 |
|---|---|
| 同一字段切两段（score 1.0、2 条） | `evidence_refs_fields=1` → 拦，置信度压到 0.4，`needs_human_review=True` |
| 两个不同字段（504 / retry 3） | `fields=2` → 放行，置信度保持 0.95 |
| 三字段但两条对不上 | 只算有效字段 → `fields=1` → 拦 |
| 三个不同字段全对 | `fields=3`、score 1.0 → 放行 |

全量 84 → 88 passed，ruff 通过。

## 6. 边界

1. **同义字段仍可凑数**。`error_message` 与 `title` 若讲的是同一件事，算两个字段但只有一个事实。
   要彻底解决得给字段分「事实类型」并去重，成本高于收益，先不做。
2. **下限抬高会多拦诚实样本**。字段少的样本（只有 `error_message` 一句）现在更容易被拦住，
   代价与 T3-2 的 HARD-018 同源：多一次人工确认，比放过编造好。
3. **不解决「引用正确、结论过度」**。这仍是 faithfulness 的活（P0-1 的 30 条标注集）。

## 7. 面试讲法

> 我的证据下限一开始数「引用条数」，后来发现它挡不住真正该挡的东西：长文本允许截断引用，
> 所以把一句 error_message 切成两段就是「两条证据」。这不是精度问题，是**度量选错了对象**——
> 有意义的不是模型说了几遍，而是有几个独立事实。所以下限改成按不同字段计数，
> 并且只数「值真的对上」的字段：字段名写得再全、值对不上也不算。
> 这类洞的共性是「指标看起来在被满足，但它量的东西不是你要的」。
