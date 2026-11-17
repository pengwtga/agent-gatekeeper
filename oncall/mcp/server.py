"""F09 MCP server：把内部只读工具暴露成标准 MCP，供外部 host（Claude Code / Cursor / 其他 agent）调用。

三条安全边界（F09 §5，写在明处）：
- 只暴露 READ（副作用分级表判）：写操作留在 CLI 本体走审批——开放写需要一整套
  身份与审计，演示价值抵不上风险，且会让「默认拒绝」这条原则显得不真诚；
- transport 只用 stdio：信任来自「谁能启动这个进程」，与「谁能执行本地命令」等价；
  开远程必须上 OAuth 2.1 + PKCE，不在本次范围；
- 错误用 isError 返回而不是异常/栈：客户端能把错误给模型看，模型才有机会改参数。

实现约 120 行，因为 ToolRegistry 的形状本来就是 MCP 的形状：
specs() ↔ tools/list，get()+validate_args() ↔ tools/call。
"""
import json
import sys
from typing import Any, Optional

from oncall.tools import MAX_RESULT_CHARS, SideEffect, side_effect_of

# MCP 协议版本：2026-07-28 修订版（无状态核心——去掉握手 session 依赖，正好匹配
# 本实现的「每行一个独立请求」模型）
PROTOCOL_VERSION = "2026-07-28"

_JSONRPC_ERRORS = {-32601: "方法不存在", -32700: "JSON 解析失败", -32603: "内部错误"}


def _input_schema(params: dict) -> dict:
    """v1 的 params 子集 → JSON Schema。registry.specs() 的形状本来就是为这步准备的。"""
    props = {k: {"type": v["type"], "description": v.get("description", "")}
             for k, v in params.items()}
    required = [k for k, v in params.items() if v.get("required")]
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _is_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


class McpServer:
    """把 ToolRegistry 暴露成 MCP server。handle() 是纯函数（可进程内测试），
    serve_forever() 只负责 stdin/stdout 的行协议搬运。"""

    def __init__(self, registry: Any, *, name: str = "oncall-mcp",
                 version: str = "0.3.0", max_chars: int = MAX_RESULT_CHARS) -> None:
        self.registry = registry
        self.name = name
        self.version = version
        self.max_chars = max_chars

    # ── 两个核心 handler（F09 §4：两个 handler 就够） ──
    def tool_schemas(self) -> list[dict]:
        out = []
        for s in self.registry.specs():
            if side_effect_of(s["name"]) is not SideEffect.READ:
                continue  # 写/危险工具永不暴露：不是配置项，是硬规则
            out.append({"name": s["name"], "description": s["description"],
                        "inputSchema": _input_schema(s["params"])})
        return out

    async def call(self, name: str, args: dict) -> dict[str, Any]:
        tool = self.registry.get(name)
        if tool is None:
            return _is_error(f"工具 {name} 不存在，可用："
                             f"{[t['name'] for t in self.tool_schemas()]}")
        if side_effect_of(name) is not SideEffect.READ:
            return _is_error(f"工具 {name} 非只读，不通过 MCP 暴露（写操作请走 oncall CLI 的审批链路）")

        from oncall.tools import call_tool

        # 复用 harness 的 call_tool：schema 校验 + 截断 + 异常转可读 error，
        # 外部 host 与自家 agent 看到的是同一份行为——不会「对外一套、对内一套」
        result = await call_tool(self.registry, name, args)
        if not result.ok:
            return _is_error(result.error)
        text = result.value if isinstance(result.value, str) \
            else json.dumps(result.value, ensure_ascii=False, default=str)
        if result.truncated:
            text += f"\n（结果已截断至 {self.max_chars} 字符，完整值 ref={result.full_ref}）"
        return {"content": [{"type": "text", "text": text}], "isError": False}

    # ── 协议分发 ──
    async def handle(self, msg: dict) -> Optional[dict]:
        method = msg.get("method")
        mid = msg.get("id")
        if method is None or str(method).startswith("notifications/"):
            return None  # 通知不回包
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": self.name, "version": self.version}}
            elif method == "tools/list":
                result = {"tools": self.tool_schemas()}
            elif method == "tools/call":
                params = msg.get("params") or {}
                result = await self.call(str(params.get("name") or ""),
                                         params.get("arguments") or {})
            elif method == "ping":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": _JSONRPC_ERRORS[-32601] + f": {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # noqa: BLE001 —— server 不能因单个请求崩掉
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603,
                              "message": f"{_JSONRPC_ERRORS[-32603]}: {type(exc).__name__}: {exc}"}}

    def serve_forever(self, *, lines: Optional[list[str]] = None, sink: Any = None) -> None:
        """行协议主循环。lines/sink 可注入（测试）；默认 stdin/stdout（真实运行）。"""
        source = iter(lines) if lines is not None else sys.stdin
        emit = sink or (lambda text: (print(text, flush=True)))
        for raw in source:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                emit(json.dumps({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700,
                                           "message": f"{_JSONRPC_ERRORS[-32700]}: {exc}"}},
                                ensure_ascii=False))
                continue
            resp = asyncio_run(self.handle(msg))
            if resp is not None:
                emit(json.dumps(resp, ensure_ascii=False))


def asyncio_run(coro: Any) -> Any:
    import asyncio
    return asyncio.run(coro)
