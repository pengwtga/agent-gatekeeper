"""run 级观测（F14）：v1 回答「这次请求慢在哪」，v2 回答「这 12 步里钱花在哪、哪步白跑」。

三条口径纪律（F14 §3/§6）：
- **approval_wait_ms 单列**：把人类思考时间算进 agent 延迟是所有带审批系统的通病——
  挂起 2 小时的 run，agent 时长只算 agent 真正跑的部分；
- 不折算钱：模型价格变动快，只报 token，读者自己乘；
- compact_count 是诚实近似：压缩发生在 context 装配时（步数 > MAX_RECENT_STEPS 后每步都在压），
  所以「被压掉的步数」= 已执行步数 - 保留的最近 3 步，下界可靠、不虚构精度。
"""

from typing import Any

from oncall.context import MAX_RECENT_STEPS

# token 成本展示名：prompt+completion 合计，评测主指标
TOTAL_TOKENS = "total_tokens"


def run_metrics(run: dict[str, Any], store: Any = None) -> dict[str, Any]:
    """从 load_run 的 run dict（可带 store 算审批等待）聚合 run 级指标。

    store 可为 None（此时 approval_wait_ms = 0，单测/无库场景可用）。
    """
    steps = run.get("steps") or []
    usage = run.get("usage") or {}
    executed = [s for s in steps if not s.skipped]
    skipped: dict[str, int] = {}
    for s in steps:
        if s.skipped:
            skipped[s.skipped] = skipped.get(s.skipped, 0) + 1

    prompt_t = int(usage.get("prompt_tokens") or 0)
    completion_t = int(usage.get("completion_tokens") or 0)

    metrics: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "steps_per_run": len(steps),
        "tool_calls_per_run": len(executed),
        "ok_calls": sum(1 for s in executed if s.ok),
        "failed_calls": sum(1 for s in executed if not s.ok),
        "skipped_breakdown": skipped,
        # F14：截断次数 = 单个工具返回太大被砍的次数，升高说明该工具返回需要瘦身
        "compact_count": max(0, len(executed) - MAX_RECENT_STEPS),
        "tokens_per_run": prompt_t + completion_t,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "wall_ms": int(usage.get("wall_ms") or 0),
    }

    # 审批等待与 agent 时长：审批等待 = 各审批 decided_at - created_at 之和（未决按当前时间）
    waits = store.approval_waits(run["run_id"]) if store is not None else []
    approval_wait_ms = int(sum(w["wait_s"] for w in waits) * 1000)
    metrics["approval_wait_ms"] = approval_wait_ms
    metrics["approval_count"] = len(waits)

    # agent_ms：总时长里扣掉等人的部分，下限 0（时钟回拨/同秒决策时防负数）
    span_ms = 0
    if run.get("created_at") and run.get("updated_at"):
        span_ms = int((run["updated_at"] - run["created_at"]) * 1000)
    metrics["agent_ms"] = max(0, span_ms - approval_wait_ms)

    # 传感器拦截率：finish 被传感器打回的比例近似 =
    # needs_human 且 reason 里有 S 前缀错误 的判定。传感器明细在 outcome.sensors，
    # 落库的是 reason 字符串——按 "S" 错误前缀统计，够用且不撒谎。
    reason = str(run.get("reason") or "")
    metrics["sensor_fail"] = any(t in reason for t in ("S1 ", "S2 ", "S3 ", "S4 ", "S5 "))
    return metrics


def sensor_fail_rate(runs: list[dict], store: Any = None) -> float:
    """多 run 聚合的传感器拦截率（F14 §3 的 sensor_fail_rate）。"""
    if not runs:
        return 0.0
    return sum(1 for r in runs if run_metrics(r, store).get("sensor_fail")) / len(runs)


def format_trace(run: dict[str, Any], store: Any = None) -> str:
    """步骤时间轴的文本渲染（CLI `oncall trace`）。每步：tool · args 摘要 · ms · ok。"""
    import json as _json

    m = run_metrics(run, store)
    lines = [
        f"run {m['run_id']}  status={m['status']}",
        f"步数 {m['steps_per_run']}（工具调用 {m['tool_calls_per_run']}，"
        f"ok {m['ok_calls']}，失败 {m['failed_calls']}）"
        f"  token {m['tokens_per_run']}  agent {m['agent_ms']}ms"
        f"  等审批 {m['approval_wait_ms']}ms（单列，不计入 agent 时长）",
    ]
    if m["skipped_breakdown"]:
        lines.append("未执行：" + ", ".join(f"{k}×{v}" for k, v in m["skipped_breakdown"].items()))
    if run.get("reason"):
        lines.append(f"原因：{run['reason']}")
    if run.get("conclusion"):
        lines.append(f"结论：{run['conclusion']}")
    lines.append("-" * 56)
    for s in run.get("steps") or []:
        args = _json.dumps(s.args, ensure_ascii=False, default=str)
        if len(args) > 60:
            args = args[:57] + "…"
        state = "ok" if s.ok else f"失败({(s.error or '')[:40]})"
        if s.skipped:
            state = f"未执行({s.skipped})"
        trunc = " [截断]" if s.truncated else ""
        lines.append(f"[{s.i:>2}] {s.tool:<18} {args:<62} {s.ms:>5}ms  {state}{trunc}")
    return "\n".join(lines)


def format_runs_table(runs: list[dict], store: Any = None) -> str:
    """run 列表（F14 面板的列表页）：run_id / 事故 / 状态 / 步数 / token / 挂起审批。"""
    lines = [f"{'run_id':<24} {'status':<16} {'steps':>5} {'tokens':>7}  goal"]
    for r in runs:
        u = r.get("usage") or {}
        tokens = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
        mark = " ⏳待审批" if r["status"] == "pending_approval" else ""
        goal = str(r.get("goal") or "")[:40]
        lines.append(f"{r['run_id']:<24} {r['status']:<16} {r['steps']:>5} {tokens:>7}  {goal}{mark}")
    return "\n".join(lines)


def percentile(values: list[float], p: float) -> float:
    """p95 等：近邻秩取法（与 v1 metrics 同口径）。values 需非空。"""
    if not values:
        return 0.0
    xs = sorted(values)
    rank = max(0, min(len(xs) - 1, round(p / 100 * (len(xs) - 1))))
    return xs[rank]
