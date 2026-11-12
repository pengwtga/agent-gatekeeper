"""LLM 客户端 — 多 Provider 容灾链（OpenAI 兼容接口通用）

设计要点（面试重点）：
1. Provider 链式容灾：primary(deepseek) → zhipu(glm-4-flash 免费) → siliconflow(免费 Qwen)，
   逐个尝试，任何一个成功即返回；全挂才抛 LLMError，由分类 Agent 降级到规则基线
2. response_format 兼容性处理：glm-4-flash 等模型对 json_object 支持不一，
   遇 400 立即去掉该参数重试，并用宽松 JSON 提取兜底（剥 markdown 代码围栏）。
   注意「能力降级」与「调用失败」是两个计数器，混用会让降级分支永远走不到（曾踩过）
3. 纯 httpx 实现，不依赖 SDK
4. 每次调用记录 in/out token（成本可见），进程内累计，由 /health 暴露
5. 未配置任何 Key 抛 LLMNotConfiguredError —— 服务永不因 LLM 挂掉而不可用
6. 请求级预算贯穿：chat_json 接收 Budget，每个 provider 每次尝试都从剩余预算里
   切时间片；切不出来就跳过 —— 总时长有上界，而不是 3 段超时互相独立的无界叠加
7. 进程级共享连接池 + 三段超时（connect/read/write 分开）：
   - 连接复用省掉每次调用的 TLS 握手（实测占总延迟的 30%+）
   - 「连不上」（connect 3s）和「连上了但算得慢」（read）必须区分：
     前者说明 provider 挂了应立即切下一家，后者才值得等待
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.budget import MIN_TRY_MS, Budget
from app.config import settings
from app.observability.obs import add_request_usage
from app.observability.tracing import span

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 调用失败。

    code 让上层能按原因归因，而不是靠解析异常里的字符串：
    - not_configured   一个 Key 都没配（部署形态，不是故障）
    - budget_exhausted 剩余预算不够发起下一次尝试（T1-1 的预期行为）
    - json_fail        模型返回了但不是合法 JSON（Prompt/模型能力问题）
    - provider_error   网络/HTTP/结构异常（provider 侧问题）
    上层按 code 打降级指标，才能区分「我们没钱等了」和「人家挂了」。
    """

    def __init__(self, message: str, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


class LLMNotConfiguredError(LLMError):
    def __init__(self, message: str, code: str = "not_configured"):
        super().__init__(message, code=code)


def _err_text(e: BaseException) -> str:
    """异常的可读描述。

    httpx 的超时异常（ReadTimeout/ConnectTimeout）str() 是空串，
    只记 str(e) 会得到 reason:""——日志里等于什么都没说，排查时只能靠猜。
    所以退化成「类名 + 有则附加消息」。
    """
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    api_key: str


class LLMClient:
    def __init__(self):
        self._chain: Optional[list[Provider]] = None
        self._client: Optional[httpx.AsyncClient] = None
        # 已确认不支持 response_format 的 provider：能力是进程级事实，记住它，
        # 后续请求直接用宽松模式，不必每个请求都先吃一次 400
        self._no_json_mode: set[str] = set()
        self._usage: dict[str, int] = {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "usage_missing": 0,   # Provider 未返回 usage 的次数（免费档常见）
        }

    @property
    def chain(self) -> list[Provider]:
        """按配置构建 Provider 容灾链（惰性构建，便于测试时热替换）"""
        if self._chain is None:
            chain: list[Provider] = []
            if settings.llm_api_key:
                chain.append(Provider("primary", settings.llm_base_url, settings.llm_model, settings.llm_api_key))
            if settings.zhipu_api_key:
                chain.append(Provider("zhipu", settings.zhipu_base_url, settings.zhipu_model, settings.zhipu_api_key))
            if settings.siliconflow_api_key:
                chain.append(Provider("siliconflow", settings.siliconflow_base_url, settings.siliconflow_model, settings.siliconflow_api_key))
            self._chain = chain
        return self._chain

    def invalidate_chain(self):
        """配置热更新后调用，强制重建容灾链"""
        self._chain = None
        self._no_json_mode.clear()

    # ── 进程级共享连接池（T1-2）──
    @property
    def client(self) -> httpx.AsyncClient:
        """共享 AsyncClient：连接复用省掉每次调用的 TLS 握手。

        默认超时只兜底「没传预算的调用」（离线评测等）；
        线上带预算的调用会在每次请求时覆盖 read 超时。
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=3.0,      # 连不上 = provider 挂了，快速失败切下一家
                    read=settings.llm_timeout_seconds,  # 算得慢，最多等这么久
                    write=10.0,
                    pool=2.0,         # 等连接池有空闲的最长时间
                ),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        return self._client

    async def aclose(self):
        """释放连接池。由 main.lifespan 在退出时调用，避免悬挂连接阻塞优雅退出。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def is_configured(self) -> bool:
        return len(self.chain) > 0

    @property
    def configured_providers(self) -> list[str]:
        return [p.name for p in self.chain]

    async def chat_json(
        self, system_prompt: str, user_payload: str,
        budget: Optional[Budget] = None,
    ) -> dict[str, Any]:
        """遍历容灾链调用 LLM，返回解析后的 JSON（带 _provider）。

        budget 不为 None 时：每个 provider 之前先检查剩余预算是否够一次最小调用，
        不够则停止遍历 —— 上层会收到 LLMError 并降级规则基线，而不是超时后被 nginx 掐断。
        全链失败才抛 LLMError。
        """
        if not self.is_configured:
            raise LLMNotConfiguredError("未配置任何 LLM Provider 的 API Key")

        errors: list[str] = []
        last_code = "budget_exhausted"
        for provider in self.chain:
            if budget is not None and budget.slice(settings.llm_timeout_per_try_ms) < MIN_TRY_MS:
                errors.append(f"{provider.name}: skipped(剩余预算不足 {budget.remaining_ms()}ms)")
                logger.warning(
                    "剩余预算不足，停止容灾遍历",
                    extra={
                        "event": "llm_chain_stop", "stage": "budget", "provider": provider.name,
                        "reason": f"remaining_ms={budget.remaining_ms()}", "outcome": "skipped",
                    },
                )
                break
            try:
                result = await self._call_with_retry(provider, system_prompt, user_payload, budget=budget)
                result["_provider"] = provider.name
                return result
            except Exception as e:
                errors.append(f"{provider.name}: {_err_text(e)}")
                last_code = getattr(e, "code", "provider_error")
                logger.warning(
                    "provider 失败，切换下一个",
                    extra={
                        "event": "llm_provider_fail", "stage": "llm", "provider": provider.name,
                        "reason": _err_text(e)[:200], "outcome": "fail",
                    },
                )
        raise LLMError(f"所有 LLM Provider 均失败: {'; '.join(errors)}", code=last_code)

    async def _call_with_retry(
        self, provider: Provider, system_prompt: str, user_payload: str,
        budget: Optional[Budget] = None,
    ) -> dict[str, Any]:
        """单 Provider 内调用：json_object 不支持时立即降级宽松提取，且不消耗重试次数。

        这里有两个独立的状态，必须分开计数：
        - use_json_mode：Provider 的能力约束，一旦不支持就永久降到宽松模式（单向）
        - failures：调用失败次数，受 llm_max_retries 约束
        把两者混成一个循环变量，就会出现「400 → continue → 下一轮仍带 response_format
        → 再 400」的死循环，降级分支永远走不到（原实现的 bug）。

        预算语义：每次尝试（含重试）都重新从剩余预算切时间片——第一次失败往往
        已经烧掉了大半预算，重试拿到的片会自动变小，而不是还按满额超时去等。
        """
        use_json_mode = provider.name not in self._no_json_mode
        failures = 0
        last_err: Optional[Exception] = None

        while failures <= settings.llm_max_retries:
            if budget is not None:
                per_try_ms = budget.slice(settings.llm_timeout_per_try_ms)
                if per_try_ms < MIN_TRY_MS:
                    raise LLMError(f"{provider.name}: 剩余预算 {per_try_ms}ms，跳过本次尝试", code="budget_exhausted")
            else:
                per_try_ms = settings.llm_timeout_seconds * 1000
            # 每次尝试一个 span：容灾链上重试几次、每次在哪个 provider 上耗多久，
            # 只看总耗时是看不出来的
            with span("llm.try", provider=provider.name, attempt=failures + 1,
                      json_mode=use_json_mode, timeout_ms=round(per_try_ms)) as s:
                try:
                    return await self._call(
                        provider, system_prompt, user_payload,
                        use_json_mode=use_json_mode, read_timeout_s=per_try_ms / 1000,
                    )
                except _JSONModeUnsupported:
                    if not use_json_mode:  # 宽松模式还报不支持，说明不是这个原因，交给上层容灾
                        raise
                    use_json_mode = False
                    self._no_json_mode.add(provider.name)
                    s["attrs"]["result"] = "json_mode_unsupported"
                    logger.info(
                        "provider 不支持 response_format=json_object，改用宽松 JSON 提取",
                        extra={
                            "event": "llm_json_mode_unsupported", "stage": "llm", "provider": provider.name,
                            "reason": "http_400_on_json_object", "outcome": "degraded",
                        },
                    )
                    continue
                except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
                    failures += 1
                    last_err = e
                    s["attrs"]["result"] = "fail"
                    logger.warning(
                        "provider 调用失败",
                        extra={
                            "event": "llm_attempt_fail", "stage": "llm", "provider": provider.name,
                            "attempt": failures, "reason": _err_text(e)[:200], "outcome": "fail",
                        },
                    )

        # last_err 必定非空：能走到这里说明至少失败过；保留兜底文案避免出现 LLMError("None")
        code = "json_fail" if isinstance(last_err, json.JSONDecodeError) else "provider_error"
        raise LLMError(
            f"{provider.name}: {_err_text(last_err) if last_err is not None else '调用失败'}",
            code=code,
        )

    async def _call(
        self, provider: Provider, system_prompt: str, user_payload: str,
        use_json_mode: bool = True, read_timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": provider.model,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
        }
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}

        t0 = time.perf_counter()
        # 共享连接池 + 每请求覆盖 read 超时（预算切片），connect/write/pool 沿用池默认
        timeout = None
        if read_timeout_s is not None:
            timeout = httpx.Timeout(
                connect=min(3.0, read_timeout_s), read=read_timeout_s,
                write=10.0, pool=2.0,
            )
        resp = await self.client.post(
            f"{provider.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if resp.status_code == 400 and use_json_mode:
            # 部分模型不支持 response_format=json_object → 宽松模式重试
            logger.info(
                "provider 返回 400（疑似不支持 json_object）",
                extra={
                    "event": "llm_http_400", "stage": "llm", "provider": provider.name,
                    "reason": resp.text[:120], "outcome": "degraded",
                },
            )
            raise _JSONModeUnsupported()
        if resp.status_code >= 400:
            # 用 HTTPStatusError 而不是自定义异常：保留「5xx 可重试」的语义，
            # 同时把服务端错误体带进异常，否则只看到 "400 Bad Request" 无法定位
            raise httpx.HTTPStatusError(
                f"{provider.name} HTTP {resp.status_code}: {resp.text[:200]}",
                request=resp.request, response=resp,
            )
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        data = resp.json()
        self._record_usage(provider, data.get("usage"))
        choice = data["choices"][0]
        truncated = choice.get("finish_reason") == "length"
        if truncated:
            # 被 max_tokens 截断时 JSON 必然不完整，提前告警比等 JSONDecodeError 更好定位
            from app.observability.metrics import metrics as _m
            _m.record_json_fail("truncated")
            logger.warning(
                "输出被 max_tokens 截断",
                extra={
                    "event": "llm_truncated", "stage": "llm", "provider": provider.name,
                    "reason": f"max_tokens={settings.llm_max_tokens}", "outcome": "fail",
                },
            )
        content = choice["message"]["content"]
        usage = data.get("usage") or {}
        logger.info(
            "LLM 调用完成",
            extra={
                "event": "llm_call", "stage": "llm", "provider": provider.name,
                "model": provider.model, "duration_ms": duration_ms,
                "tokens": int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0),
                "outcome": "ok",
            },
        )
        return _extract_json(content, provider=provider.name)

    # ── 成本可见：token 计数（进程级，多 worker 需换 Prometheus 计数器）──
    def _record_usage(self, provider: Provider, usage: Optional[dict[str, Any]]) -> None:
        from app.observability.metrics import metrics  # 延迟导入：metrics 不依赖 llm_client，但保持模块加载顺序简单

        self._usage["calls"] += 1
        if not usage:
            self._usage["usage_missing"] += 1
            logger.info(
                "token 用量未返回",
                extra={
                    "event": "llm_usage_missing", "stage": "llm", "provider": provider.name,
                    "model": provider.model, "outcome": "degraded",
                },
            )
            return
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        self._usage["prompt_tokens"] += prompt_tokens
        self._usage["completion_tokens"] += completion_tokens
        add_request_usage(prompt_tokens, completion_tokens)   # 记到当前请求账上（T2-4）
        metrics.record_tokens(prompt_tokens, completion_tokens)  # 记到最近一小时窗口
        logger.info(
            "token 记账",
            extra={
                "event": "llm_usage", "stage": "llm", "provider": provider.name,
                "model": provider.model, "tokens": prompt_tokens + completion_tokens,
                "outcome": "ok",
            },
        )

    @property
    def usage(self) -> dict[str, int]:
        """进程累计用量快照，供 /health 暴露（测试断言也用它）"""
        return dict(self._usage)

    def reset_usage(self) -> None:
        for k in self._usage:
            self._usage[k] = 0


class _JSONModeUnsupported(Exception):
    pass


def _extract_json(content: str, provider: str = "unknown") -> dict[str, Any]:
    """宽松 JSON 提取：直接解析 → 剥代码围栏 → 截取首个平衡花括号块

    每种失败都按类型计数（T2-3 的 json_parse_fail）：
    - no_json / invalid_json：模型根本没给 JSON，或给了半截
    - code_fence_invalid / brace_invalid：找到了包裹但内容不合法
    「救回来」的次数也记（json_recovered）：它比失败率更早预警 ——
    一个 provider 从 0 次变成常常要靠剥围栏，说明它开始不守格式了，
    此时还没失败，但已经在恶化。
    """
    from app.observability.metrics import metrics

    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fenced:
        try:
            result = json.loads(fenced.group(1))
        except json.JSONDecodeError:
            metrics.record_json_fail("code_fence_invalid")
        else:
            metrics.record_json_recovered("code_fence")
            logger.info(
                "JSON 靠剥代码围栏救回",
                extra={"event": "llm_json_recovered", "stage": "llm", "provider": provider,
                       "reason": "code_fence", "outcome": "degraded"},
            )
            return result

    brace = re.search(r"\{.*\}", content, re.DOTALL)
    if brace:
        try:
            result = json.loads(brace.group(0))
        except json.JSONDecodeError:
            metrics.record_json_fail("brace_invalid")
        else:
            metrics.record_json_recovered("brace_slice")
            logger.info(
                "JSON 靠截取花括号块救回",
                extra={"event": "llm_json_recovered", "stage": "llm", "provider": provider,
                       "reason": "brace_slice", "outcome": "degraded"},
            )
            return result

    metrics.record_json_fail("no_json" if brace is None and fenced is None else "invalid_json")
    raise json.JSONDecodeError("no json found", content, 0)


# 模块级单例
llm_client = LLMClient()
