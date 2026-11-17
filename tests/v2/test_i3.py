"""I3 测试：MCP server（F09）/ MCP client（F08）/ agentic RAG（F12）。

子进程测试只测「函数测不到的东西」：真进程 stdio 往返、外部 host 视角。
协议细节、信任边界、检索循环全部进程内（毫秒级、零网络）。
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

from oncall.api import RunOptions, run_incident
from oncall.budget import RunBudget  # noqa: F401 —— 供 rag stub 复用口径
from oncall.mcp.client import McpClient, ServerSpec, merge_mcp_tools, trust_server
from oncall.mcp.server import McpServer
from oncall.policy import APPROVAL, decide
from oncall.rag_loop import MAX_ROUNDS, agentic_retrieve, register_rag_tool, strip_noise
from oncall.tools import SideEffect, call_tool, side_effect_of
from tests.v2.stubs import FakeClock, ScriptedLLM, build_registry

# ── F12：agentic RAG ──────────────────────────────────────────────

def test_strip_noise_removes_ids_and_dates():
    assert strip_noise("XF-885 注册卡单 2026-11-17 处理") == "注册卡单 处理"
    assert strip_noise("审批回调异常") == "审批回调异常"


class _FakeRag:
    """检索桩：按 query 是否含噪声词决定返回空还是命中，用于驱动改写路径。"""

    def __init__(self, noisy_hits, clean_hits):
        self.noisy_hits = noisy_hits
        self.clean_hits = clean_hits
        self.queries: list[str] = []
        self.spec = SimpleNamespace(threshold=0.4, id_field="ticket_id")

    @staticmethod
    def _admitted(h, threshold):
        return (h.get("score") or 0) >= threshold

    async def _retrieve(self, q, top_k, budget=None):
        self.queries.append(q)
        if "XF-" in q or "2026" in q:
            return self.noisy_hits
        return self.clean_hits


_GOOD_HITS = [
    {"ticket_id": "ONCALL-001", "title": "审批通过但无注册任务", "symptom": "xflow 审批通过后注册任务没生成",
     "resolution": "补建注册任务并核对回调", "root_cause_class": "xflow_callback_lost", "score": 0.9},
    {"ticket_id": "ONCALL-002", "title": "审批已完成缺任务", "symptom": "审批完成缺任务",
     "resolution": "查回调日志", "root_cause_class": "xflow_callback_lost", "score": 0.7},
]


def test_agentic_rewrite_recovers_from_noisy_query():
    """首轮带单号召回为空 → 删噪声词重检 → 命中。这是 F12 最典型的一条路径。"""
    rag = _FakeRag(noisy_hits=[], clean_hits=_GOOD_HITS)

    r = asyncio.run(agentic_retrieve(rag, "XF-885 注册卡单 2026-11-17 处理"))

    assert r.enough is True
    assert r.rounds == 2
    assert r.rewrites == ["删除内部单号/时间戳噪声词"]
    assert "XF-" not in rag.queries[1]


def test_agentic_stops_when_nothing_to_rewrite():
    """库外问题（召回恒空、query 又没有噪声词）：一轮就停，不空转到 3 轮。"""
    rag = _FakeRag(noisy_hits=[], clean_hits=[])

    r = asyncio.run(agentic_retrieve(rag, "完全无关的问题"))

    assert r.enough is False
    assert r.rounds == 1
    assert r.rewrites == []


def test_enough_requires_two_field_groups():
    """只有现象字段、没有动作/根因 → 不算够（稀疏 schema 的前向兼容）。"""
    rag = _FakeRag(noisy_hits=[], clean_hits=[{"title": "x", "symptom": "y", "score": 0.9}])
    r = asyncio.run(agentic_retrieve(rag, "xflow 审批通过但任务未创建"))
    assert r.enough is False


def test_never_exceeds_max_rounds():
    """每轮都「能改写」（标题一直在变）→ 必须被轮数上限拦住，不能无限改下去。"""

    class _RotatingRag:
        def __init__(self):
            self.n = 0
            self.spec = SimpleNamespace(threshold=0.4)

        @staticmethod
        def _admitted(h, threshold):
            return (h.get("score") or 0) >= threshold

        async def _retrieve(self, q, top_k, budget=None):
            self.n += 1
            return [{"title": f"变体{self.n}", "symptom": f"s{self.n}", "resolution": "r",
                     "root_cause_class": "c", "score": 0.1}]

    r = asyncio.run(agentic_retrieve(_RotatingRag(), "查不到的东西"))

    assert r.rounds == MAX_ROUNDS and r.enough is False


def test_rag_search_tool_is_read_and_works():
    rag = _FakeRag(noisy_hits=[], clean_hits=_GOOD_HITS)
    reg = build_registry(("kb_stats", {}, SimpleNamespace()))
    register_rag_tool(reg, rag, clock=FakeClock())

    assert side_effect_of("rag_search") is SideEffect.READ

    result = asyncio.run(call_tool(reg, "rag_search", {"query": "审批通过但任务没生成"}))
    assert result.ok
    payload = json.loads(json.dumps(result.value)) if isinstance(result.value, dict) else result.value
    assert payload["enough"] is True and payload["hits"][0]["class"] == "xflow_callback_lost"


def test_real_kb_offline_retrieval_is_enough():
    """真知识库 + 无 LLM（关键词序）：标准措辞的问题一轮就该够——防「体检说能召回、线上召不回」。"""
    from app.agents.rag_assistant import RagAssistant
    from app.core.llm_client import llm_client
    from app.domains.ssrm import pack as ssrm_pack

    rag = RagAssistant(llm_client, ssrm_pack.assistant)
    asyncio.run(rag.warmup())

    r = asyncio.run(agentic_retrieve(
        rag, "xflow 审批已通过但是注册任务一直没有创建，回调好像丢了"))

    assert r.enough is True
    assert r.hits and r.hits[0]["root_cause_class"] == "xflow_callback_lost"


# ── F09：MCP server ──────────────────────────────────────────────

def _serve_registry():
    rag = SimpleNamespace(docs=[{"root_cause_class": "xflow_callback_lost"},
                                {"root_cause_class": "ea_sync_timeout"}],
                          mode="keyword")
    from app.core.builtin_tools import build_default_registry

    return build_default_registry(rag)


def test_server_exposes_only_read_tools():
    reg = _serve_registry()
    reg.register(__import__("app.core.tools", fromlist=["Tool"]).Tool(
        name="restart_service", description="重启服务", params={}, handler=_async_ok,
        read_only=False))

    names = {t["name"] for t in McpServer(reg).tool_schemas()}

    assert "restart_service" not in names
    assert {"kb_stats", "query_incidents", "system_topology"} <= names


async def _async_ok(**_):
    return {}


def test_server_protocol_roundtrip_in_process():
    reg = _serve_registry()
    server = McpServer(reg)
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "{不是 JSON",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "kb_stats", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "query_incidents", "arguments": {}}}),  # 缺必填
        json.dumps({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"}),
    ]
    out: list[str] = []
    server.serve_forever(lines=lines, sink=out.append)

    assert len(out) == 6  # 通知不回包
    msgs = [json.loads(m) for m in out]
    assert msgs[0]["result"]["protocolVersion"] == "2026-07-28"
    assert msgs[1]["id"] is None and msgs[1]["error"]["code"] == -32700  # 坏 JSON
    tools = {t["name"] for t in msgs[2]["result"]["tools"]}
    assert "kb_stats" in tools
    assert msgs[3]["result"]["isError"] is False
    assert "缺少必填参数" in msgs[4]["result"]["content"][0]["text"]  # 可读错误，不是栈
    assert msgs[5]["error"]["code"] == -32601


# ── F08：MCP client ──────────────────────────────────────────────

def _serve_cmd():
    return [sys.executable, "-m", "oncall", "serve"]


def _client_env():
    env = dict(os.environ)
    env.setdefault("ONCALL_DB", "/tmp/oncall_test_unused.sqlite3")
    return env


def test_client_server_roundtrip_via_subprocess():
    """外部 host 视角：真进程 stdio → list → call → 错误可读。这是 F09 验收第 1 条。"""

    async def _main():
        client = McpClient(ServerSpec("ssrm", _serve_cmd()))
        await client.start()
        try:
            tools = {t["name"] for t in await client.list_tools()}
            ok = await client.call_tool("kb_stats", {})
            bad = await client.call_tool("query_incidents", {})
            missing = await client.call_tool("nope", {})
            return tools, ok, bad, missing
        finally:
            await client.aclose()

    tools, ok, bad, missing = asyncio.run(_main())

    assert "kb_stats" in tools and "rag_search" in tools  # serve 注册表含 F12 工具
    assert ok["ok"] is True and "total" in ok["value"]
    assert bad["ok"] is False and "缺少必填参数" in bad["value"]
    assert missing["ok"] is False and "不存在" in missing["value"]


def test_merge_prefixes_prevent_collision_and_fail_closed():
    """两个 server 都有 kb_stats → 前缀防覆盖；外部工具默认 WRITE（fail-closed）。"""

    async def _main():
        reg = build_registry()
        failed = await merge_mcp_tools(reg, [
            ServerSpec("a", _serve_cmd()),
            ServerSpec("b", _serve_cmd()),
        ])
        names = sorted(s["name"] for s in reg.specs())
        # 外部工具默认按 WRITE 处理（不在 SIDE_EFFECTS 里 = fail-closed 兜底）
        se_a = side_effect_of("mcp__a__kb_stats")
        decision = decide("mcp__a__kb_stats", se_a, 0.95)
        # 显式信任后降级
        trusted = trust_server("a", SideEffect.READ, registry=reg)
        return failed, names, se_a, decision, trusted

    failed, names, se_a, decision, trusted = asyncio.run(_main())

    assert failed == []
    assert names.count("mcp__a__kb_stats") == 1 and names.count("mcp__b__kb_stats") == 1
    assert se_a is SideEffect.WRITE
    assert decision.kind == APPROVAL  # 即使置信度 0.95 也要审批——协议不担保行为
    assert trusted >= 1 and side_effect_of("mcp__a__kb_stats") is SideEffect.READ


def test_merge_degrades_when_server_unreachable():
    async def _main():
        reg = build_registry()
        return await merge_mcp_tools(reg, [ServerSpec("ghost", [sys.executable, "-c", "import sys; sys.exit(1)"])])

    failed = asyncio.run(_main())
    assert failed == ["ghost"]


# ── 集成：rag_search 进入主 loop 的 run ──────────────────────────

def test_run_incident_can_use_rag_search_tool():
    """工具进了主 loop 的注册表就该能被跑通（用脚本 LLM，不打真模型）。"""
    rag = _FakeRag(noisy_hits=[], clean_hits=_GOOD_HITS)
    from app.core.builtin_tools import build_default_registry

    reg = build_default_registry(rag)
    register_rag_tool(reg, rag, clock=FakeClock())

    script = [
        {"action": "tool", "tool": "rag_search", "args": {"query": "审批通过但任务没生成"},
         "thought": "先查知识库", "confidence": 0.9},
        {"action": "tool", "tool": "kb_stats", "args": {},
         "thought": "补一个独立来源（断言根因需要跨源证据）", "confidence": 0.9},
        {"action": "finish",
         "conclusion": "这是 xflow 回调丢失，补建注册任务即可",
         "root_cause_class": "xflow_callback_lost",
         "evidence": [
             {"field": "rag_search", "ref": "r-test#step:1", "quote": "补建注册任务并核对回调"},
             {"field": "kb_stats", "ref": "r-test#step:2", "quote": "xflow_callback_lost: 30"},
         ],
         "confidence": 0.85},
    ]

    deps = SimpleNamespace(llm=ScriptedLLM(script), registry=reg, clock=FakeClock(),
                           store=None, observer=None)

    result = asyncio.run(run_incident("审批过了但任务没生成", deps=deps,
                                      opts=RunOptions(max_steps=4, budget_ms=30_000)))

    assert result.outcome.status == "ok"
    assert result.outcome.root_cause_class == "xflow_callback_lost"
