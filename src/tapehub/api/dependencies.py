"""FastAPI 依赖：从 app.state 获取配置与服务实例。"""

from fastapi import Request

from tapehub.application.bar_query_service import BarQueryService
from tapehub.application.capability_service import CapabilityService
from tapehub.providers.registry import ProviderRegistry
from tapehub.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def get_capability_service(request: Request) -> CapabilityService:
    return request.app.state.capability_service


def get_bar_query_service(request: Request) -> BarQueryService:
    return request.app.state.bar_query_service


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")
