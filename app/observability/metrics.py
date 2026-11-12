"""进程内指标（T2-3 / T2-4）——滑动窗口，不引 Prometheus

为什么不上 Prometheus：单实例 + 一个人维护，多一个 exporter 就多一个要盯的东西，
而它解决的问题（多实例聚合、长期存储）当前不存在。代价写清楚在 §6 边界里：
进程重启归零、多 worker 各算各的。

只用标准库：deque 存样本，排序取分位数。2000 个样本排序约 0.2ms，
放在请求路径上可以忽略；真要上量再换直方图桶（固定内存、可合并）。
"""

import time
from collections import Counter, deque
from threading import Lock
from typing import Optional

from app.config import settings

# 分位数用「最近的秩」法：样本少时它比线性插值诚实（不会给出一个不存在的值）
_PERCENTILES = (50, 95, 99)

# 分钟桶保留 60 个 = 最近 1 小时。为什么是分钟粒度而不是秒：
# 秒级要 3600 个点，前端画不下、传输也浪费；而运维看趋势关心的是「哪一分钟开始变坏」。
_SERIES_MINUTES = 60
# 最近请求流水保留条数。够翻一屏、够回答「刚才那条慢请求是什么」，再多就该查日志了。
_RECENT_MAX = 50


class Metrics:
    def __init__(self, window_s: Optional[int] = None, max_samples: Optional[int] = None):
        self.window_s = window_s if window_s is not None else settings.metrics_window_seconds
        self._lat: deque[tuple[float, float]] = deque(maxlen=max_samples or settings.metrics_max_samples)
        self._lock = Lock()

        # 分钟级时序：只存「可累加的量」（计数、耗时求和/最大值、token），
        # 不存每分钟的延迟样本列表 —— 那是 60 × N 个 float，随流量线性涨，
        # 而它的唯一用途是画历史分位数。分位数当前值由滑动窗口给（snapshot().latency_ms）。
        self._series: deque[dict] = deque()
        # 最近请求流水：给面板上的「请求表」用，trace_id 在这里第一次变得可点
        self._recent: deque[dict] = deque(maxlen=_RECENT_MAX)

        # 累计计数（不随窗口滚动，重启才归零）
        self._requests = 0
        self._ok = 0
        self._client_error = 0
        self._server_error = 0
        self._excluded = 0  # /health 这类探针请求，只计数不进分位数

        self._degrade: Counter[str] = Counter()
        self._json_fail: Counter[str] = Counter()
        self._json_recovered: Counter[str] = Counter()
        self._confidence: Counter[str] = Counter()
        # 根因分类的输出分布。unknown 是分类体系的覆盖率指标：
        # 它涨了不一定是模型坏了，更可能是「出现了体系外的新故障模式」——
        # 这是新增根因类别的第一手信号（治理闭环见 docs/design-decisions.md）
        self._classes: Counter[str] = Counter()

        # token 只保留最近 1 小时（成本看的是「最近花得快不快」，不是历史总账）
        self._tokens: deque[tuple[float, int, int]] = deque()
        self._tokens_total = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0}

    # ── 写入 ──
    def _bucket(self, now: float) -> dict:
        """取当前分钟桶，没有就新建（调用方必须已持锁）。

        必须容忍「业务层先记降级、中间件后记请求」的顺序：record_degrade 会比
        record_request 早发生，所以建桶不能只放在 record_request 里，否则每分钟
        第一笔降级会丢——而它往往正是最关键的那笔。
        """
        minute = int(now // 60)
        if self._series and self._series[-1]["m"] == minute:
            return self._series[-1]
        self._series.append({
            "m": minute, "count": 0, "excluded": 0, "client_error": 0, "server_error": 0,
            "degrade": 0, "lat_sum": 0.0, "lat_max": 0.0,
            "prompt_tokens": 0, "completion_tokens": 0,
        })
        while len(self._series) > _SERIES_MINUTES:
            self._series.popleft()
        return self._series[-1]

    def record_request(self, *, path: str, method: str, status: int,
                       duration_ms: float, outcome: str, counted: bool = True,
                       trace_id: str = "-", tokens: int = 0) -> None:
        now = time.time()
        with self._lock:
            self._requests += 1
            if outcome == "ok":
                self._ok += 1
            elif outcome == "client_error":
                self._client_error += 1
            elif outcome == "server_error":
                self._server_error += 1
            b = self._bucket(now)
            b["count"] += 1
            if outcome == "client_error":
                b["client_error"] += 1
            elif outcome == "server_error":
                b["server_error"] += 1
            if counted:
                self._lat.append((now, duration_ms))
                b["lat_sum"] += duration_ms
                b["lat_max"] = max(b["lat_max"], duration_ms)
                # 探针请求不进流水：面板每 5s 拉一次 /metrics，放进来会把
                # 真正要看的业务请求全挤掉，50 条缓冲只剩自己
                self._recent.append({
                    "ts": time.strftime("%H:%M:%S", time.localtime(now)),
                    "trace_id": trace_id, "method": method, "path": path,
                    "status": status, "duration_ms": duration_ms,
                    "outcome": outcome, "tokens": tokens,
                })
            else:
                self._excluded += 1
                b["excluded"] += 1

    def record_degrade(self, level: str) -> None:
        """记录一次「通道决策」的结果：none / budget_exhausted / provider_error / not_configured

        none 也要记：降级构成里需要它当分母，看「降级占比」才知道能力退了多少。
        但它不计入分钟桶的 degrade 计数 —— 桶里那一列表达的是「这一分钟有多少笔降级」，
        把正常走通也加进去，折线图会变成一条恒等于请求量的直线。
        """
        with self._lock:
            self._degrade[level] += 1
            if level != "none":
                self._bucket(time.time())["degrade"] += 1

    def record_json_fail(self, kind: str) -> None:
        with self._lock:
            self._json_fail[kind] += 1

    def record_json_recovered(self, kind: str) -> None:
        """宽松提取救回来的次数：这个数高说明模型输出不稳定，比 fail 更早预警"""
        with self._lock:
            self._json_recovered[kind] += 1

    def record_confidence(self, confidence: Optional[float]) -> None:
        if confidence is None:
            return
        c = max(0.0, min(1.0, float(confidence)))
        bucket = f"{int(c * 10) / 10:.1f}"
        with self._lock:
            self._confidence[bucket] += 1

    def record_class(self, root_cause_class: str) -> None:
        """记录一次根因分类的输出类别（LLM 通道与规则基线都算）。

        和 record_confidence 只统计 LLM 通道不同：unknown 在规则基线里
        同样可能出现（兜底匹配不上），漏记规则通道的 unknown 会低估漏斗入口。
        """
        with self._lock:
            self._classes[root_cause_class] += 1

    def record_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        now = time.time()
        with self._lock:
            self._tokens.append((now, prompt_tokens, completion_tokens))
            self._tokens_total["prompt_tokens"] += prompt_tokens
            self._tokens_total["completion_tokens"] += completion_tokens
            self._tokens_total["llm_calls"] += 1
            b = self._bucket(now)
            b["prompt_tokens"] += prompt_tokens
            b["completion_tokens"] += completion_tokens
            self._prune_tokens(now)

    def _prune_tokens(self, now: float) -> None:
        cutoff = now - 3600
        while self._tokens and self._tokens[0][0] < cutoff:
            self._tokens.popleft()

    # ── 读取 ──
    def _percentiles(self, now: float) -> dict[str, float]:
        cutoff = now - self.window_s
        samples = sorted(ms for ts, ms in self._lat if ts >= cutoff)
        if not samples:
            return {f"p{p}": 0.0 for p in _PERCENTILES} | {"max": 0.0}
        out: dict[str, float] = {}
        for p in _PERCENTILES:
            idx = min(len(samples) - 1, max(0, round(p / 100 * len(samples)) - 1))
            out[f"p{p}"] = round(samples[idx], 1)
        out["max"] = round(samples[-1], 1)
        return out

    def _series_out(self, now: float) -> list[dict]:
        """补齐最近 60 分钟（调用方必须已持锁）。

        为什么补零而不是「有什么就返回什么」：没有请求的那一分钟不会建桶，
        如果直接跳过，前端的时间轴就会被压缩——图上看着一直有流量，
        实际是 14:03 之后停了 20 分钟。补齐后 X 轴才是均匀时间。
        """
        by_minute = {b["m"]: b for b in self._series}
        current = int(now // 60)
        out: list[dict] = []
        for m in range(current - _SERIES_MINUTES + 1, current + 1):
            b = by_minute.get(m)
            if b is None:
                out.append({
                    "ts": m * 60, "label": time.strftime("%H:%M", time.localtime(m * 60)),
                    "count": 0, "qps": 0.0, "errors": 0, "error_rate": 0.0,
                    "degrade": 0, "degrade_rate": 0.0,
                    "lat_avg": 0.0, "lat_max": 0.0, "tokens": 0,
                })
                continue
            count = b["count"]
            errors = b["client_error"] + b["server_error"]
            # 分母口径与 snapshot().degrade.rate 保持一致：扣掉探针请求。
            # 两处不一致的话，面板上「降级率 5%」和折线图的柱子会对不上，
            # 看的人第一反应是数据错了，而不是口径不同。
            business = max(0, count - b["excluded"])
            out.append({
                "ts": b["m"] * 60,
                "label": time.strftime("%H:%M", time.localtime(b["m"] * 60)),
                "count": count,
                "qps": round(count / 60, 3),
                "errors": errors,
                "error_rate": round(errors / business, 4) if business else 0.0,
                "degrade": b["degrade"],
                "degrade_rate": round(b["degrade"] / business, 4) if business else 0.0,
                "lat_avg": round(b["lat_sum"] / count, 1) if count else 0.0,
                "lat_max": round(b["lat_max"], 1),
                "tokens": b["prompt_tokens"] + b["completion_tokens"],
            })
        return out

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._prune_tokens(now)
            lat = self._percentiles(now)
            hour_in = sum(t[1] for t in self._tokens)
            hour_out = sum(t[2] for t in self._tokens)
            hours_count = len(self._tokens) or 1
            # by_level 里含 none（正常走通），所以「降级笔数」= 总数 - none。
            # 曾直接拿总数当降级数，面板上出现过「降级率 57%，但构成里只有 none」的自相矛盾。
            decisions = sum(self._degrade.values())
            degraded = decisions - self._degrade.get("none", 0)
            # 分母只算业务请求：探针（/health）每 30s 一次，把它算进去会让降级率
            # 被稀释成「看起来还好」，而真实业务的失败率高得多
            business = max(0, self._requests - self._excluded)
            return {
                "window_seconds": self.window_s,
                "requests": {
                    "total": self._requests,
                    "ok": self._ok,
                    "client_error": self._client_error,
                    "server_error": self._server_error,
                    "excluded_from_latency": self._excluded,
                },
                "latency_ms": lat,
                "degrade": {
                    "total": degraded,
                    "decisions": decisions,
                    "denominator": business,
                    "rate": round(degraded / business, 4) if business else 0.0,
                    "by_level": dict(self._degrade),
                },
                "json": {
                    "fail_total": sum(self._json_fail.values()),
                    "fail_by_kind": dict(self._json_fail),
                    "recovered_by_kind": dict(self._json_recovered),
                },
                "confidence_hist": dict(sorted(self._confidence.items())),
                "classification": self._classification_out(),
                "series": {
                    "window_minutes": _SERIES_MINUTES,
                    "points": self._series_out(now),
                },
                "recent_requests": list(self._recent)[::-1],  # 最新在前
                "tokens": {
                    "last_hour": {"prompt": hour_in, "completion": hour_out, "total": hour_in + hour_out},
                    "last_hour_calls": len(self._tokens),
                    "avg_per_call": round((hour_in + hour_out) / hours_count, 1) if self._tokens else 0.0,
                    "process_total": dict(self._tokens_total),
                },
            }

    def _classification_out(self) -> dict:
        """分类输出分布。unknown_rate 是「体系覆盖率」的反向指标：
        它反映的是体系外故障模式的流入速度，不是模型质量——
        所以归因要分开看：模型坏了看降级率，体系旧了看 unknown 率。"""
        total = sum(self._classes.values())
        unknown = self._classes.get("unknown", 0)
        return {
            "decisions": total,
            "unknown_count": unknown,
            "unknown_rate": round(unknown / total, 4) if total else 0.0,
            "by_class": dict(self._classes.most_common()),
        }

    def reset(self) -> None:
        with self._lock:
            self._lat.clear()
            self._tokens.clear()
            self._series.clear()
            self._recent.clear()
            self._requests = self._ok = self._client_error = self._server_error = self._excluded = 0
            for c in (self._degrade, self._json_fail, self._json_recovered, self._confidence, self._classes):
                c.clear()
            for k in self._tokens_total:
                self._tokens_total[k] = 0


metrics = Metrics()
