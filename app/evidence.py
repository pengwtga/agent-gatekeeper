"""证据可验证性校验（evidence grounding）

要解决的问题：LLM 会给出「看起来合理」的判断理由。置信度是模型自报的，
越会编的模型越敢给 0.95，所以「模型说它很有把握」不构成任何保证。

这一层做的事：把 evidence 里的锚点（错误码、英文标识、数字、中文词片段）
逐个拿到输入原文里找，算出「可回溯比例」。它是白盒的——任何一条判断都能
人工复核；且不额外调用 LLM（零成本、零延迟）。

定位：门禁，不是裁判。
- 分数低 → 压置信度、强制转人工（宁可不答，不可错答）
- 分数高 → 只说明「理由引用的是真实输入」，不说明「结论正确」

已知边界（被追问时主动说，比装完美可信）：
1. 同义改写会漏判：模型说「审批早已完成」而输入是「状态 APPROVED」，字面不重合
2. 中文按 2-gram 匹配，短句容易天然命中 → 锚点太少时直接判 0，交给人工
3. 只能抓「凭空编造」，抓不到「引用正确但推理错误」，后者只能靠评测集
"""

import re
from typing import Any

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


def evidence_score(evidence: str, incident: dict[str, Any]) -> float:
    """evidence 有多少能回溯到输入原文。0~1；< min_evidence_score 视为无据。

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
