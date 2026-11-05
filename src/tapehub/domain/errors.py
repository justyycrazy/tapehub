"""统一领域错误。

错误只携带业务码、安全消息与 details，不感知 HTTP 状态码；
状态码映射由 API 层的 exception_handlers 完成。
"""

from enum import Enum
from typing import Any, ClassVar


class ErrorCode(str, Enum):
    """统一业务错误码。"""

    INVALID_REQUEST = "INVALID_REQUEST"
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    PROVIDER_NOT_IMPLEMENTED = "PROVIDER_NOT_IMPLEMENTED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    NO_PROVIDER_AVAILABLE = "NO_PROVIDER_AVAILABLE"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    # 实时快照首包未就绪（订阅已建立，上游数据未到）
    QUOTE_NOT_READY = "QUOTE_NOT_READY"
    # 行情流客户端数达到上限（服务容量闸门）
    STREAM_LIMIT_REACHED = "STREAM_LIMIT_REACHED"
    # 上游并发订阅 symbol 数达到计划上限（如 Twelve Data WS credits）
    UPSTREAM_SUBSCRIPTION_LIMIT = "UPSTREAM_SUBSCRIPTION_LIMIT"
    # 以下为框架补充错误码（非业务链路产生）
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class TapehubError(Exception):
    """tapehub 统一错误基类。"""

    code: ClassVar[ErrorCode]
    default_message: ClassVar[str] = "Request failed."

    def __init__(
        self, message: str | None = None, *, details: dict[str, Any] | None = None
    ) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(self.message)


class InvalidRequestError(TapehubError):
    code = ErrorCode.INVALID_REQUEST
    default_message = "Invalid request parameters."


class ProviderNotFoundError(TapehubError):
    code = ErrorCode.PROVIDER_NOT_FOUND
    default_message = "Provider not found."


class ProviderDisabledError(TapehubError):
    code = ErrorCode.PROVIDER_DISABLED
    default_message = "Provider is disabled."


class ProviderNotImplementedError(TapehubError):
    code = ErrorCode.PROVIDER_NOT_IMPLEMENTED
    default_message = "Provider is not implemented."


class UnsupportedCapabilityError(TapehubError):
    code = ErrorCode.UNSUPPORTED_CAPABILITY
    default_message = "Requested capability is not supported by the provider."


class NoProviderAvailableError(TapehubError):
    code = ErrorCode.NO_PROVIDER_AVAILABLE
    default_message = "No provider available for the requested capability."


class ConcurrencyLimitError(TapehubError):
    """网关侧并发闸门：共享运行时的在飞请求已达上限（防脚本失控）。"""

    code = ErrorCode.CONCURRENCY_LIMIT
    default_message = "Provider concurrency limit reached, retry later."


class UpstreamTimeoutError(TapehubError):
    code = ErrorCode.UPSTREAM_TIMEOUT
    default_message = "Upstream provider timed out."


class UpstreamRateLimitedError(TapehubError):
    code = ErrorCode.UPSTREAM_RATE_LIMITED
    default_message = "Upstream provider rate limited."


class UpstreamError(TapehubError):
    code = ErrorCode.UPSTREAM_ERROR
    default_message = "Upstream provider error."


class QuoteNotReadyError(TapehubError):
    """实时快照未就绪：订阅已发起，但上游首包未在超时窗口内到达。

    常见原因：标的不存在/无权限（上游不推送）、非交易时段上游静默、
    或网络/上游故障。可稍后重试。
    """

    code = ErrorCode.QUOTE_NOT_READY
    default_message = "Quote snapshot is not ready yet."


class StreamLimitReachedError(TapehubError):
    """行情流客户端上限：并发 SSE 连接数已达服务配置上限。

    属服务容量闸门（与 CONCURRENCY_LIMIT 同为可重试限流），稍后重试或
    先断开空闲连接。
    """

    code = ErrorCode.STREAM_LIMIT_REACHED
    default_message = "Streaming client limit reached, retry later."


class UpstreamSubscriptionLimitError(TapehubError):
    """上游订阅上限：并发订阅 symbol 数已达上游计划上限。

    2026-11-05 Twelve Data WS 探针实证：WS credit = 当前活跃订阅 symbol 数
    （免费版 8 个），退订/断连即时释放。可重试限流：空闲退订释放额度后
    重试，或提升上游计划档位并调大对应网关配置。
    """

    code = ErrorCode.UPSTREAM_SUBSCRIPTION_LIMIT
    default_message = "Upstream concurrent subscription limit reached, retry later."


class ManifestError(TapehubError):
    """Provider manifest 加载或校验失败（启动期错误，正常不出现在请求链路）。"""

    code = ErrorCode.INTERNAL_ERROR
    default_message = "Provider manifest is invalid."
