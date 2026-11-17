"""上下文装配（I2：压缩已实现）。

压缩策略（F04 §3）：**摘要成事实，不留摘要成散文**——
- 事实表（③ 段）全量保留且每条带 ref：它是压缩的产物，不是压缩的对象
- 步骤原文只留最近 3 步，更早的从上下文移出（原文不删，full_ref 可回查 trace）
- 规则段（system）钉在头部，永不参与压缩——规则被压掉，模型会直接调写工具
"""

import json
from typing import Iterable, Sequence

from oncall.tools import ToolCallRecord

SYSTEM_PROMPT = """你是 SSRM（供应商管理系统）值班团队的排障助手。

工作方式：
1. 先看已确认事实，判断还缺什么；每次只调一个工具，不要重复查同一样东西。
2. **已确认事实足以回答任务时，必须直接输出 finish**，不要再调工具确认一遍。
3. 只能调用工具清单里的工具，参数只给声明过的字段。
4. 证据必须来自工具返回，不要凭经验断言。至少覆盖两个不同来源（如日志 + 配置）。
5. 涉及写操作或不可逆动作（重启、删除、改配置）时，输出 ask_human，不要自己动手。
6. 信息不足时输出 ask_human，并说明缺哪一项——「不知道」是可接受的结论。

输出必须是 JSON，二选一：
调工具：{"action":"tool","tool":"名称","args":{},"thought":"一句话：为什么查这个","confidence":0.7}
        （confidence 必填，0~1 的匹配程度；低于 0.7 不会被执行）
结束：  {"action":"finish","conclusion":"一句话根因+建议动作","root_cause_class":"类名或unknown",
        "evidence":[{"field":"log","ref":"...","quote":"原文片段"}],"confidence":0.0}
转人工：{"action":"ask_human","reason":"缺什么/为什么不敢下结论"}"""

MAX_RECENT_STEPS = 3
MAX_FACT_CHARS = 200


def _facts_block(facts: Iterable[dict]) -> str:
    rows = []
    for f in facts:
        value = str(f.get("value", ""))
        if len(value) > MAX_FACT_CHARS:
            value = value[:MAX_FACT_CHARS] + "…"
        rows.append(f"- {f.get('field')} = {value}  （来源 {f.get('ref', '-')}）")
    return "\n".join(rows) or "- （暂无）"


def _steps_block(steps: Sequence[ToolCallRecord]) -> str:
    rows = []
    if len(steps) > MAX_RECENT_STEPS:
        rows.append(f"- （更早 {len(steps) - MAX_RECENT_STEPS} 步已压缩："
                    f"结论在「已确认事实」段，原文见 trace 的 full_ref）")
    for s in steps[-MAX_RECENT_STEPS:]:
        state = "ok" if s.ok else f"失败({s.error[:60]})"
        if s.skipped:
            state = f"未执行({s.skipped})"
        rows.append(f"[{s.i}] {s.tool} args={json.dumps(s.args, ensure_ascii=False)[:120]} → {state}")
    return "\n".join(rows) or "- （还没查任何东西）"


def build_messages(
    goal: str,
    facts: Sequence[dict],
    steps: Sequence[ToolCallRecord],
    tool_specs: Sequence[dict],
) -> tuple[str, str]:
    """返回 (system, user)。为什么不拼成一个字符串：v1 的 chat_json 就是双参数接口，
    且 system 稳定才有前缀缓存可吃。"""
    payload = (
        f"## 任务\n{goal}\n\n"
        f"## 已确认事实\n{_facts_block(facts)}\n\n"
        f"## 最近步骤\n{_steps_block(steps)}\n\n"
        f"## 工具清单\n{json.dumps(list(tool_specs), ensure_ascii=False)}\n"
    )
    return SYSTEM_PROMPT, payload
