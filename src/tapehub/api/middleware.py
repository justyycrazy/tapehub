"""请求上下文与入站限流中间件。

- RequestContextMiddleware：生成 request_id、回写响应头、记录精简访问日志。
- ClientRateLimitMiddleware：入站固定窗口限流（审查项 M3），保护上游付费
  配额——网关侧既有闸门（线程池/SSE 客户端数/tqsdk inflight）只保护网关
  自身资源，不保护上游按次计费配额。

日志只记录方法、路径、状态码、耗时与 request_id；
不记录查询串、响应体和任何 Provider 密钥。
"""

import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from tapehub.api.exception_handlers import error_response
from tapehub.domain.errors import ErrorCode

logger = logging.getLogger("tapehub.access")

# 限流豁免路径：/health 为连接测试用途、不访问上游（与鉴权豁免一致）
_RATE_LIMIT_EXEMPT_PATHS = frozenset({"/health"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 未处理异常向上穿透（正常由内层异常处理器兜住转 500，穿到这里
            # 属处理器自身异常等边缘路径）：补记访问行再重抛，避免 5xx 请求
            # 在访问日志中缺失（只有异常堆栈、无方法/路径/耗时）
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "%s %s -> 500 (%.1f ms) request_id=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                request_id,
            )
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
        # 未捕获异常会在上面抛出（由最外层处理器统一转换为 500）。
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s -> %s (%.1f ms) request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response


class ClientRateLimitMiddleware(BaseHTTPMiddleware):
    """入站客户端固定窗口限流（TAPEHUB_CLIENT_RATE_LIMIT_PER_MINUTE）。

    身份 = 命中配置白名单的 ``X-Api-Key``，未命中/未配置一律归入
    ``anonymous``——桶数上界 = len(api_keys) + 1，随机 Key 不产生新桶。
    窗口 60 秒；超限返回 429 ``CLIENT_RATE_LIMITED`` + ``Retry-After``。

    单进程 single 模式设计：计数为进程内存（不跨进程、不持久化）；仅
    事件循环访问，无锁。注册在 RequestContextMiddleware 内层，限流 429
    请求同样获得 request_id 与访问日志行。
    """

    _WINDOW_SECONDS = 60.0
    _ANONYMOUS = "anonymous"

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        limit_per_minute: int,
        configured_keys: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)
        self._limit = limit_per_minute
        self._key_set = frozenset(configured_keys)
        # identity -> (窗口起点 monotonic, 已计数请求数)；过期窗口惰性重置
        self._buckets: dict[str, tuple[float, int]] = {}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _RATE_LIMIT_EXEMPT_PATHS:
            return await call_next(request)
        identity = self._identity(request)
        now = time.monotonic()
        window_start, count = self._buckets.get(identity, (now, 0))
        if now - window_start >= self._WINDOW_SECONDS:
            window_start, count = now, 0
        if count >= self._limit:
            retry_after = max(
                1, math.ceil(window_start + self._WINDOW_SECONDS - now)
            )
            response = error_response(
                request,
                ErrorCode.CLIENT_RATE_LIMITED,
                "Client rate limit exceeded, retry later.",
                {"limit": self._limit, "window_seconds": int(self._WINDOW_SECONDS)},
                status_code=429,
            )
            response.headers["Retry-After"] = str(retry_after)
            return response
        self._buckets[identity] = (window_start, count + 1)
        return await call_next(request)

    def _identity(self, request: Request) -> str:
        supplied = request.headers.get("X-Api-Key", "")
        return supplied if supplied in self._key_set else self._ANONYMOUS
