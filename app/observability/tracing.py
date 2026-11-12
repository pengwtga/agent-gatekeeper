"""请求内 span 收集 + 最近链路存储

trace_id 有了（T2-1）之后，能回答「这一次请求的日志在哪」，但答不了「时间花在哪」。
一次 `/assist` 里其实有检索、重排、LLM 生成三段，串行加起来 8s，
只知道总耗时没法判断该优化谁——这才是链路追踪要解决的问题。

设计上刻意做小：

1. **不引 OpenTelemetry**。OTel 要 collector、要 exporter、要 span 上下文传播规范，
   一个人维护的单实例服务付不起这个成本。这里只要能画出「本进程内一次请求的阶段耗时」。
   真要接 OTel 时，`span()` 的调用点是现成的（换实现即可，业务代码不用动）。

2. **只收当前请求的 span**，用 contextvar 而不是参数透传。理由同 T2-1：
   调用链是 路由 → 分类器 → LLM 客户端 → httpx，逐层加参数会漏。

3. **flat 列表，不做父子层级**。当前最深的链路只有 3 层，平铺 + start_ms 就能看出
   串行/嵌套关系（子 span 完全落在父 span 区间内）。层级树要额外维护 parent 引用，
   收益在当前深度下不明显。

边界（写在明处）：
- 进程内内存，重启归零；
- 只保留最近 100 条链路，滚出去的查不到；
- `asyncio.gather` 起的并发子任务里 `set` 不会回传到父上下文，那些 span 会丢——
  当前代码全是顺序 await，没有并发分支。
"""

import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Any, Optional

_spans: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar("spans", default=None)
_started: ContextVar[float] = ContextVar("spans_started", default=0.0)
# 当前所在的 span（不是列表，是「栈顶」）。用来记 parent/depth：
# 只靠名字里的点号猜层级是不够的 —— `llm.try` 嵌在 `rag.answer` 里，
# 两者点号数一样，起点也几乎相同，排序时谁在前就只能看运气。
_current_span: ContextVar[Optional[dict[str, Any]]] = ContextVar("current_span", default=None)

# 最多留多少条链路。和 metrics 的最近请求流水（50 条）不同量级：
# 一条链路约 10 个 span × 200B，100 条也就 200KB
_MAX_TRACES = 100


def begin_spans() -> None:
    """中间件入口调用：给当前请求开一个空的 span 列表"""
    _spans.set([])
    _started.set(time.perf_counter())
    _current_span.set(None)


def collect_spans() -> list[dict[str, Any]]:
    """中间件出口调用：取走本请求收集到的 span（浅拷贝，之后 contextvar 会被 reset）

    排序键带层级：同一时刻开始的父 span（assist）必须排在子 span（rag.retrieve）前面。
    只按 start_ms 排的话，亚毫秒请求里父子起点几乎相同，顺序会随机跳动，
    瀑布图上看起来像子阶段包住了父阶段。depth 来自真实的嵌套关系（见 span()），
    不是从名字里数点号 —— 名字是给人看的，不能当结构用。
    """
    items = _spans.get() or []
    return sorted(items, key=lambda s: (s["start_ms"], s.get("depth", 0), -s["duration_ms"]))


@contextmanager
def span(name: str, **attrs: Any):
    """记录一段耗时。

    用法：
        with span("llm.try", provider=p.name, attempt=n) as s:
            ...
            s["attrs"]["status"] = 200

    `yield` 出来的是 dict，调用方可以在块内补 attrs（比如最终 attempt 次数），
    这样打点信息能在真实结果出来之后再填，不用提前猜。
    """
    started_at = time.perf_counter()
    origin = _started.get() or started_at
    parent = _current_span.get()
    record: dict[str, Any] = {
        "name": name,
        "start_ms": round((started_at - origin) * 1000, 1),
        "duration_ms": 0.0,
        "status": "ok",
        # depth 决定瀑布图的缩进与排序，parent 便于排查「这个 span 属于谁」
        "depth": (parent["depth"] + 1) if parent else 0,
        "parent": parent["name"] if parent else None,
        "attrs": {k: v for k, v in attrs.items() if v is not None},
    }
    token = _current_span.set(record)
    try:
        yield record
    except BaseException as e:  # 含 CancelledError：被掐断的请求也要留痕
        record["status"] = "error"
        record["attrs"]["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        raise
    finally:
        record["duration_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
        _current_span.reset(token)
        bucket = _spans.get()
        if bucket is not None:
            bucket.append(record)


class TraceStore:
    """按 trace_id 存最近 N 条链路。OrderedDict 而不是 dict：
    需要「先进先出淘汰」，dict 从 3.7 起保序但没有 popitem(last=False)。"""

    def __init__(self, maxlen: int = _MAX_TRACES):
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._maxlen = maxlen
        self._lock = Lock()

    def put(self, trace_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._items[trace_id] = record
            self._items.move_to_end(trace_id)
            while len(self._items) > self._maxlen:
                self._items.popitem(last=False)

    def get(self, trace_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._items.get(trace_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


traces = TraceStore()
