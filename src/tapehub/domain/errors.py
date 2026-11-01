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
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
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


class UpstreamTimeoutError(TapehubError):
    code = ErrorCode.UPSTREAM_TIMEOUT
    default_message = "Upstream provider timed out."


class UpstreamRateLimitedError(TapehubError):
    code = ErrorCode.UPSTREAM_RATE_LIMITED
    default_message = "Upstream provider rate limited."


class UpstreamError(TapehubError):
    code = ErrorCode.UPSTREAM_ERROR
    default_message = "Upstream provider error."


class ManifestError(TapehubError):
    """Provider manifest 加载或校验失败（启动期错误，正常不出现在请求链路）。"""

    code = ErrorCode.INTERNAL_ERROR
    default_message = "Provider manifest is invalid."
