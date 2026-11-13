"""证据可验证性校验（evidence grounding）

要解决的问题：LLM 会给出「看起来合理」的判断理由。置信度是模型自报的，
越会编的模型越敢给 0.95，所以「模型说它很有把握」不构成任何保证。

**两个判据，先后关系明确**：

1. `check_refs` / `refs_score`：**结构化引用**（T3-5，主判据）。
   要求模型输出 `evidence_refs: [{"field": ..., "value": ...}]`，
   校验退化成两步 —— 字段存在吗？值相等吗？得分 = 有效引用 / 总引用，与文本长短无关。
2. `evidence_score`：**自由文本字面匹配**（T0-03，兜底）。
   `evidence_refs` 缺失（模型不按 schema 输出）时退回它，保证老模型不掉功能。

为什么要有第二个：schema 是请求，不是保证。小模型可能不吐 `evidence_refs`，
这时不能因为「没按格式来」就判它无据——那是把格式合规当成事实真伪。

定位：门禁，不是裁判。
- 分数低 → 压置信度、强制转人工（宁可不答，不可错答）
- 分数高 → 只说明「理由引用的是真实输入」，不说明「结论正确」

已知边界（被追问时主动说，比装完美可信）：
1. 字面匹配会漏判同义改写：模型说「审批早已完成」而输入是「状态 APPROVED」，字面不重合
   （结构化引用的价值就在这里：它不看措辞，只看字段和值）
2. 中文按 2-gram 匹配，短句容易天然命中 → 锚点太少时直接判 0，交给人工
3. 两个判据都抓不到「引用正确、结论过度」（样本 G：引真错误码，结论跳到「EA 整体不可用」），
   那是蕴含判断，只能靠评测集或 NLI，不在本模块能力范围内
"""

import json
import re
from typing import Any, Optional

# 编码类锚点：EA_TIMEOUT / FT20261025088 / 504 这类，最不容易被"顺手编对"
_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}|\d{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]+")
_SEPARATOR = re.compile(r"[\s，。、；：？！“”‘’（）()【】\[\]{}《》〈〉,;\"'`]+")

# 编码类锚点权重高：它出现在输入里几乎不可能是巧合
_ASCII_WEIGHT = 3.0
_CJK_WEIGHT = 1.0
_MIN_ANCHORS = 3  # 锚点太少 → 分数没有统计意义，直接判无据

# 无区分度的中文 2-gram：任何运维描述里都可能出现，命中不代表"引用了输入"
_CJK_STOP = {
    "这个", "那个", "可能", "因为", "所以", "说明", "判断", "建议", "需要", "进行",
    "出现", "导致", "问题", "异常", "情况", "我们", "可以", "应该", "已经", "还是",
    "就是", "没有", "一个", "以及", "如果", "那么", "但是", "然后", "这样", "不是",
    "的是", "是的", "目前", "一般", "通常", "之时", "之后", "之前", "其中", "由于",
}


def _normalize(text: str) -> str:
    """去掉空白与标点、统一小写；保留 _ - . 以便 ea_timeout 这类标识符能整串比对"""
    return _SEPARATOR.sub("", str(text)).lower()


def _flatten(obj: Any) -> list[str]:
    """把 incident 里的键与值全部摊平成一个字符串池（evidence 引用字段名也算有效引用）"""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            out.extend(_flatten(v))
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            out.extend(_flatten(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def _anchors(evidence: str) -> list[tuple[str, float]]:
    """抽取可核对的锚点，返回 [(锚点, 权重)]，去重保序"""
    found: list[tuple[str, float]] = [(t.lower(), _ASCII_WEIGHT) for t in _ASCII_TOKEN.findall(evidence)]

    for run in _CJK_RUN.findall(evidence):
        if len(run) < 2:
            continue
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in _CJK_STOP:
                found.append((gram, _CJK_WEIGHT))

    dedup: dict[str, float] = {}
    for token, weight in found:
        dedup.setdefault(token, weight)
    return list(dedup.items())


# ───────────────────────── 主判据：结构化引用（T3-5） ─────────────────────────

_MISSING = object()
_MIN_SUBSTRING = 8  # 长文本允许「截断引用」（引用原文的一段），但太短的碎片不算引用


def _resolve(incident: dict[str, Any], field: str) -> Any:
    """按 `a.b.0.c` 路径取值；取不到返回 _MISSING。

    为什么支持嵌套与下标：领域包的 incident 是嵌套结构（incident.error_code、
    items.0.sku），校验器必须能走到同样的位置，否则合法引用会被判成「字段不存在」。
    """
    cur: Any = incident
    for part in str(field).split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
    return cur


def _value_matches(actual: Any, claimed: Any) -> bool:
    """引用的值是否等于输入里的值。三条规则，都有明确的"为什么"：

    1. 数字按数值比（JSON 里 `504` 与 `"504"`、`4` 与 `4.0` 是同一个事实）
    2. 字符串按归一化后比（去空白标点、小写）：`EA_TIMEOUT` 与 `ea_timeout` 是同一件事实
    3. 长文本允许被引用为原文子串：模型引用 `error_message` 时截断是合理的，
       但**改写不行** —— 子串关系能保证引用的字都在原文里
    """
    if actual is None or claimed is None:
        return actual is None and claimed is None
    if isinstance(actual, bool) or isinstance(claimed, bool):
        return str(actual).strip().lower() == str(claimed).strip().lower()
    if isinstance(actual, (int, float)):
        try:
            return abs(float(actual) - float(claimed)) < 1e-9
        except (TypeError, ValueError):
            return False
    if isinstance(actual, (dict, list)):
        return _normalize(str(actual)) == _normalize(str(claimed))
    a, c = _normalize(actual), _normalize(claimed)
    if not c:
        return False
    return a == c or (len(c) >= _MIN_SUBSTRING and c in a)


def check_refs(refs: Any, incident: dict[str, Any]) -> list[dict[str, Any]]:
    """逐条核对 `evidence_refs`，返回明细（直接透给前端渲染「字段 → 值」引用条）。

    明细里带 `ok` / `actual` / `reason`，是因为这一层的卖点是可审计：
    值班同学看到的不是一句「证据不可信」，而是「你引用的 db_pool_usage 这个字段根本不在输入里」。
    """
    detail: list[dict[str, Any]] = []
    if isinstance(refs, str):
        # 小模型偶尔把数组二次编码成字符串，能解析就解析，解析不了当作没给
        try:
            refs = json.loads(refs)
        except (ValueError, TypeError):
            return detail
    if not isinstance(refs, list):
        return detail

    for ref in refs:
        if not isinstance(ref, dict):
            continue
        field = str(ref.get("field") or "").strip()
        if not field:  # 没有 field 的条目不是引用，不参与打分（也不惩罚，它只是噪音）
            continue
        claimed = ref.get("value")
        actual = _resolve(incident, field)

        if actual is _MISSING:
            detail.append({"field": field, "value": claimed, "ok": False, "reason": "字段不存在"})
        elif "value" not in ref:
            detail.append({"field": field, "value": None, "ok": False, "actual": actual,
                           "reason": "缺少 value"})
        elif _value_matches(actual, claimed):
            detail.append({"field": field, "value": claimed, "ok": True, "actual": actual})
        else:
            detail.append({"field": field, "value": claimed, "ok": False, "actual": actual,
                           "reason": "值不相等"})
    return detail


def refs_score(detail: list[dict[str, Any]]) -> Optional[float]:
    """有效引用 / 总引用。没有任何可用引用时返回 None —— 表示「这个判据用不上」，
    调用方必须回退字面匹配，不能当成 0 分（0 分会把「模型没按 schema 输出」误判成「编造」）。"""
    if not detail:
        return None
    return round(sum(1 for d in detail if d["ok"]) / len(detail), 3)


def evidence_score(evidence: str, incident: dict[str, Any]) -> float:
    """【兜底判据】evidence 自由文本有多少字能回溯到输入原文。0~1；< min_evidence_score 视为无据。

    只在 `evidence_refs` 不可用时使用（T3-5）。它测的是「引用密度」而不是「证据质量」，
    实测存在分数反转（只丢两个错误码 1.000 > 引四个字段讲通顺 0.684），所以不能当质量分，
    只能做「有没有据」的二分门禁；根治办法是结构化引用，见上面的 check_refs 一段。

    纯字符串比对，不依赖模型自省，也不依赖第二次 LLM 调用（那只是把说谎的成本翻倍）。

    阈值 0.35 是在 10 条人工构造样本上标定的（见 docs/optimization/T0-03）：
    诚实引用 0.476~0.684，纯编造 0.000~0.167，半编造（借一个真错误码讲通用结论）0.400。
    它只保证"抓得住凭空编造"，抓不住"引用正确但结论过度"——后者要靠评测集。
    """
    anchors = _anchors(evidence or "")
    if not anchors:
        return 0.0

    pool = _normalize(" ".join(_flatten(incident)))
    total = sum(w for _, w in anchors)
    hit = sum(w for token, w in anchors if token in pool)

    # 极简引用（如 "EA_TIMEOUT / 504"）：锚点太少不构成统计意义，
    # 但"全部命中"本身就是强证据，"全不中"则直接判无据
    if len(anchors) < _MIN_ANCHORS:
        return 1.0 if hit == total else 0.0

    return round(hit / total, 3)
