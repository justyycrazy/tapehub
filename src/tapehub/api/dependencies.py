"""FastAPI 依赖：从 app.state 获取配置与服务实例，及可选入站认证。"""

import secrets

from fastapi import Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from tapehub.application.bar_query_service import BarQueryService
from tapehub.application.capability_service import CapabilityService
from tapehub.application.instrument_resolve_service import InstrumentResolveService
from tapehub.application.quote_query_service import QuoteQueryService
from tapehub.application.quote_stream_service import QuoteStreamService
from tapehub.providers.registry import ProviderRegistry
from tapehub.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_api_key(request: Request) -> None:
    """可选入站认证：``TAPEHUB_API_KEYS`` 非空即校验 ``X-Api-Key`` 请求头。

    - 未配置（空列表）：直接放行——single 单用户模式默认无鉴权，行为与
      历史版本完全一致；
    - 已配置：头缺失或不匹配返回 401（统一错误结构）；比较经
      ``secrets.compare_digest`` 防时序侧信道。比较在 bytes 上进行：
      ``compare_digest`` 的 str 形态要求 ASCII（非 ASCII 输入抛 TypeError，
      会被兜底处理器误转为 500），而请求头字节经 latin-1 解码可产生任意
      非 ASCII 字符串，bytes 化保证任意客户端输入都收敛为 401。
    """
    keys = request.app.state.settings.api_keys
    if not keys:
        return
    supplied = request.headers.get("X-Api-Key", "")
    supplied_bytes = supplied.encode("utf-8", errors="replace")
    if not any(
        secrets.compare_digest(supplied_bytes, key.encode("utf-8")) for key in keys
    ):
        raise StarletteHTTPException(status_code=401)


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def get_capability_service(request: Request) -> CapabilityService:
    return request.app.state.capability_service


def get_instrument_resolve_service(request: Request) -> InstrumentResolveService:
    return request.app.state.instrument_resolve_service


def get_bar_query_service(request: Request) -> BarQueryService:
    return request.app.state.bar_query_service


def get_quote_query_service(request: Request) -> QuoteQueryService:
    return request.app.state.quote_query_service


def get_quote_stream_service(request: Request) -> QuoteStreamService:
    return request.app.state.quote_stream_service


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")
