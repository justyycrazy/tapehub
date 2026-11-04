"""FastAPI 应用工厂与组合根。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tapehub.api.exception_handlers import register_exception_handlers
from tapehub.api.middleware import RequestContextMiddleware
from tapehub.api.routes import capabilities, health, market
from tapehub.application.bar_query_service import BarQueryService
from tapehub.application.capability_service import CapabilityService
from tapehub.application.provider_router import ProviderRouter
from tapehub.providers.registry import ProviderRegistry, build_default_registry
from tapehub.settings import Settings


def create_app(
    settings: Settings | None = None,
    registry: ProviderRegistry | None = None,
) -> FastAPI:
    """构建应用。

    ``registry`` 参数供测试注入 Fake Provider；
    默认构建内置 Provider 注册表（不要求安装任何 Provider SDK）。
    """
    settings = settings or Settings()
    registry = registry or build_default_registry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # 停机：关闭持有长连接的适配器运行时（鸭子类型，不感知具体 Provider）
        for record in app.state.registry.list_records():
            shutdown = getattr(record.adapter, "shutdown_runtime", None)
            if shutdown is not None:
                shutdown()

    app = FastAPI(
        title="tapehub",
        version=settings.version,
        description="通用行情数据网关（历史 K 线查询；tqsdk 实时行情接入中）",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.capability_service = CapabilityService(registry)
    app.state.bar_query_service = BarQueryService(ProviderRouter(registry))

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(market.router)
    return app


app = create_app()
