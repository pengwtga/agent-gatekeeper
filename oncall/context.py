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
1. 先看已确认事实，判断还缺什么；每次只调一个工具。**同一个工具+相同参数只调一次**——
   结果已经在事实段里，重复调用不会得到新信息；需要别的信息就换工具或换参数。
2. **已确认事实足以回答任务时，必须直接输出 finish**，不要再调工具确认一遍。
3. 只能调用工具清单里的工具，参数只给声明过的字段。
4. 证据必须来自工具返回，不要凭经验断言。**要给出具体根因类（root_cause_class ≠ unknown）
   时，evidence 必须同时包含两个来源：kb（rag_search 的条目）+ history（query_incidents 的
   同类工单）**——只有 topology/stats 或单源不足以断言根因。
5. 涉及写操作或不可逆动作（重启、删除、清空、改配置）时，输出 ask_human，不要自己动手。
6. **「还没有事实」不是 ask_human 的理由——那正是该用工具收集信息的信号。**
   先用工具查（查知识库/查历史工单），只有工具也拿不到、或需要人补充系统外信息时才 ask_human。
7. rag_search 返回 enough=false（知识库无可信依据）时：finish 且 root_cause_class=unknown，
   结论如实说明「库内无依据」——**不许编造类名**。「不知道」是可接受的结论。
8. 若出现「## 上一轮被门禁退回」段：那是系统否决了你上一次 finish。按它指出的缺口补齐
   （缺哪个来源就去查哪个工具），再重新 finish；**不要重复提交同一份结论**。
9. 事实段每条写作 `- 工具名 = 内容（来源 …）`，evidence.field 按下表换算：
   rag_search→kb、query_incidents→history、system_topology→topology、kb_stats→stats。

输出必须是 JSON，三选一：
调工具：{"action":"tool","tool":"名称","args":{},"thought":"一句话：为什么查这个","confidence":0.7}
        （confidence 必填，0~1 的匹配程度；低于 0.7 不会被执行）
结束：  {"action":"finish","conclusion":"一句话根因+建议动作","root_cause_class":"类名（见任务里的类别清单）或unknown",
        "evidence":[{"field":"来源标签","ref":"工具名#步数","quote":"工具返回的原文片段"}],"confidence":0.0}
        evidence.field 固定取值：kb=知识库检索(rag_search)、history=历史工单(query_incidents)、
        topology=系统拓扑(system_topology)、stats=知识库统计(kb_stats)
转人工：{"action":"ask_human","reason":"缺什么/为什么不敢下结论"}"""

MAX_RECENT_STEPS = 3
MAX_FACT_CHARS = 200

_CATALOG_CACHE: str | None = None


def default_class_catalog() -> str:
    """根因类别清单（懒加载 + 缓存）。oncall 允许依赖 app.domains（架构守卫白名单）。
    类目描述截到「处置：」前——catalog 只负责让模型会选类名，处置知识在 KB 里。"""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        try:
            from app.domains.ssrm.taxonomy import ROOT_CAUSE_CLASSES

            _CATALOG_CACHE = "\n".join(
                f"- {name}: {desc.split('。处置')[0]}"
                for name, desc in ROOT_CAUSE_CLASSES.items())
        except Exception:  # pragma: no cover —— 域包缺失时退化为无清单（桩测试环境）
            _CATALOG_CACHE = ""
    return _CATALOG_CACHE


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
    memory_block: str = "",
    class_catalog: str = "",
    feedback: str = "",
) -> tuple[str, str]:
    """返回 (system, user)。为什么不拼成一个字符串：v1 的 chat_json 就是双参数接口，
    且 system 稳定才有前缀缓存可吃。

    memory_block（F15）：事故记忆段，插在事实之后、步骤之前——它回答「以前怎么解决的」，
    地位低于本次工具取回的事实（标注了「不得作为证据引用」）。

    class_catalog（真模型实测后补）：finish 的 root_cause_class 必须有合法取值可选。
    桩 LLM 时代没暴露这个问题——脚本里写什么类名就是什么类名；真模型只能猜，
    30 条里 28 条类名不符。教训：**评测暴露的 prompt 缺陷，修在装配层而不是 judge 层**。

    feedback（F17）：上一次 finish 被验收传感器退回的原因 + 已做的自动补齐。
    放在最后一段——模型对上下文末尾的指令依从度最高，而这正是「必须马上修正」的指令。
    """
    memory_section = f"## 历史同类\n{memory_block}\n\n" if memory_block else ""
    class_section = f"## 根因类别清单\n{class_catalog}\n\n" if class_catalog else ""
    feedback_section = f"## 上一轮被门禁退回\n{feedback}\n\n" if feedback else ""
    payload = (
        f"## 任务\n{goal}\n\n"
        f"## 已确认事实\n{_facts_block(facts)}\n\n"
        f"{memory_section}"
        f"## 最近步骤\n{_steps_block(steps)}\n\n"
        f"{class_section}"
        f"{feedback_section}"
        f"## 工具清单\n{json.dumps(list(tool_specs), ensure_ascii=False)}\n"
    )
    return SYSTEM_PROMPT, payload
