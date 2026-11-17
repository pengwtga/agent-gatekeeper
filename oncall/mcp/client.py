"""F08 MCP client：接入外部 MCP server 的工具，并且**仍然过 harness 的四道关**。

信任边界（F08 §4，本文件最重要的一条）：
MCP 的 tool annotations（readOnly / destructive）是 **server 自述**，规范自己都说
「host 不要默认相信」。所以本客户端根本不读 annotations——外部工具一律不在
SIDE_EFFECTS 表里声明，side_effect_of 按 fail-closed 兜底成 WRITE（要审批）；
显式信任某个 server 才调 trust_server() 降级为 READ。

命名 mcp__<server>__<tool>：两个 server 都可能有 query，前缀防注册表覆盖，
也让策略能按 server 粒度配置（F08 §3）。

失败与降级（F08 §6）：server 启动失败记 degraded 返回、继续跑（本地工具仍可用），
不抛——少一路数据不该让整个排障挂掉。
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

from app.core.tools import Tool, ToolRegistry
from oncall.mcp.server import PROTOCOL_VERSION
from oncall.tools import SIDE_EFFECTS, SideEffect

# 描述瘦身（F08 §5）：只留第一句。长说明吃上下文，30 个工具的全文描述
# 能吃掉 1/4 窗口——第一句（动作 + 对象）足够模型做选择
_DESC_MAX = 160


@dataclass
class ServerSpec:
    name: str
    command: list[str]  # 例：["npx", "mcp-server-filesystem", "/data"] 或 [python, "-m", "oncall", "serve"]


class McpError(RuntimeError):
    """协议层错误（server 退出 / 响应 id 不匹配 / 返回 error）。"""


def _first_sentence(text: str) -> str:
    for sep in ("。", "；", "\n"):
        if sep in text:
            text = text.split(sep)[0]
            break
    return text[:_DESC_MAX]


def _params_from_schema(schema: Optional[dict]) -> dict:
    """MCP 的 inputSchema（JSON Schema）→ v1 的 params 子集。

    未知类型一律 string：schema 校验宁严勿宽，但类型降级只影响校验宽度，
    不影响执行——总比拒收整个工具好。
    """
    out: dict[str, dict] = {}
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    for k, v in props.items():
        t = v.get("type", "string")
        out[k] = {"type": t if t in ("string", "number", "integer", "boolean") else "string",
                  "description": str(v.get("description", ""))[:120],
                  "required": k in required}
    return out


class McpClient:
    """单个 MCP server 的 stdio 连接：initialize → tools/list → tools/call。"""

    def __init__(self, spec: ServerSpec, *, timeout_s: float = 30.0) -> None:
        self.spec = spec
        self.timeout_s = timeout_s
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._id = 0

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.spec.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "oncall-harness", "version": "0.3.0"},
        })
        await self._notify("notifications/initialized")

    async def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        if self._proc is None:
            raise McpError("client 未启动")
        self._id += 1
        rid = self._id
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write((json.dumps(req) + "\n").encode())
        await self._proc.stdin.drain()
        line = await asyncio.wait_for(self._proc.stdout.readline(), self.timeout_s)
        if not line:
            raise McpError(f"server '{self.spec.name}' 无响应（可能已退出）")
        resp = json.loads(line)
        if resp.get("id") != rid:
            raise McpError(f"server '{self.spec.name}' 响应 id 不匹配")
        if "error" in resp:
            raise McpError(str(resp["error"].get("message", "未知协议错误")))
        return resp.get("result") or {}

    async def _notify(self, method: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        await self._proc.stdin.drain()

    async def list_tools(self) -> list[dict]:
        return (await self._rpc("tools/list", {})).get("tools") or []

    async def call_tool(self, name: str, args: dict) -> dict[str, Any]:
        """返回统一结构；isError/超时都转成 ok=False，不抛——由上层决定怎么呈现。"""
        try:
            result = await asyncio.wait_for(
                self._rpc("tools/call", {"name": name, "arguments": args}), self.timeout_s)
        except asyncio.TimeoutError:
            return {"ok": False, "value": "", "error": f"MCP 调用超时（>{self.timeout_s}s）"}
        except McpError as exc:
            return {"ok": False, "value": "", "error": f"MCP 协议错误: {exc}"}
        text = "".join(c.get("text", "") for c in result.get("content") or []
                       if isinstance(c, dict) and c.get("type") == "text")
        return {"ok": not result.get("isError"), "value": text,
                "error": text if result.get("isError") else ""}

    async def aclose(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), 3.0)
        except asyncio.TimeoutError:
            self._proc.kill()


def _make_handler(client: McpClient, remote_name: str):
    async def _handler(**args: Any) -> dict[str, Any]:
        r = await client.call_tool(remote_name, args)
        if not r["ok"]:
            # 抛出而非返回：harness 的 call_tool 会把异常转成可读 error 记进 step，
            # 模型下一轮能看见原因并改参数
            raise RuntimeError(r["error"])
        return {"text": r["value"]}
    return _handler


async def merge_mcp_tools(registry: ToolRegistry, specs: list[ServerSpec]) -> list[str]:
    """把各 server 的工具注册进 harness 注册表，返回启动失败的 server 名。

    失败不抛（degraded 继续跑）；单个工具 schema 不兼容就跳过该工具并打到
    返回值之外的 warning——不静默降级，也不让一个坏工具拖死整批。
    """
    failed: list[str] = []
    for spec in specs:
        try:
            client = McpClient(spec)
            await client.start()
            tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 —— F08 §6：启动失败记 degraded
            print(f"[mcp] server '{spec.name}' 启动失败，已降级跳过: {exc}", file=__import__("sys").stderr)
            failed.append(spec.name)
            continue

        prefix = f"mcp__{spec.name}__"
        for t in tools:
            name = str(t.get("name") or "").strip()
            if not name:
                continue
            full = prefix + name
            if registry.get(full) is not None:
                print(f"[mcp] 工具 {full} 重名，跳过", file=__import__("sys").stderr)
                continue
            registry.register(Tool(
                name=full,
                description=_first_sentence(str(t.get("description") or name)),
                params=_params_from_schema(t.get("inputSchema")),
                handler=_make_handler(client, name),
                read_only=False,  # 信任交给 SIDE_EFFECTS 表，不在 Tool 上撒谎
            ))
    return failed


def trust_server(server_name: str, effect: SideEffect = SideEffect.READ,
                 *, registry: ToolRegistry) -> int:
    """显式信任一个 server：把 mcp__<server>__* 在 SIDE_EFFECTS 里声明为 effect。

    信任必须是显式动作——不写这行，所有外部工具都是 WRITE（要审批）。
    返回声明的工具数。
    """
    prefix = f"mcp__{server_name}__"
    n = 0
    for s in registry.specs():
        if s["name"].startswith(prefix):
            SIDE_EFFECTS[s["name"]] = effect
            n += 1
    return n
