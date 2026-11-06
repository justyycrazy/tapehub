"""FastAPI 依赖：从 app.state 获取配置与服务实例。"""

from fastapi import Request

from tapehub.application.bar_query_service import BarQueryService
from tapehub.application.capability_service import CapabilityService
from tapehub.application.instrument_resolve_service import InstrumentResolveService
from tapehub.application.quote_query_service import QuoteQueryService
from tapehub.application.quote_stream_service import QuoteStreamService
from tapehub.providers.registry import ProviderRegistry
from tapehub.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


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
