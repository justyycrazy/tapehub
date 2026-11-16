"""FastAPI 应用工厂与组合根。"""

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI

from tapehub.api.exception_handlers import register_exception_handlers
from tapehub.api.middleware import RequestContextMiddleware
from tapehub.api.routes import capabilities, health, instruments, market, quotes
from tapehub.application.bar_query_service import BarQueryService
from tapehub.application.capability_service import CapabilityService
from tapehub.application.instrument_resolve_service import InstrumentResolveService
from tapehub.application.provider_router import ProviderRouter
from tapehub.application.quote_query_service import QuoteQueryService
from tapehub.application.quote_stream_service import QuoteStreamService
from tapehub.providers.registry import ProviderRegistry, build_default_registry
from tapehub.settings import Settings

logger = logging.getLogger(__name__)


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

    # 服务路径：让 tapehub.* 生命周期日志（如 tqsdk runtime 代际/回收）可见
    logging.basicConfig(level=logging.INFO)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 线程池容量：call_in_thread（全部线程型上游调用）共享 anyio 默认
        # limiter。anyio 默认 limiter 是 per-event-loop 的 RunVar，必须在本
        # loop 的 lifespan 启动阶段设置（create_app 模块级无 loop 可用）；
        # 挂死上游调用占满容量会拖垮全部线程型 Provider，见
        # settings.thread_pool_size 注释
        anyio.to_thread.current_default_thread_limiter().total_tokens = (
            app.state.settings.thread_pool_size
        )
        yield
        # 停机顺序：先终结行情流（释放订阅引用，客户端收到明确事件），
        # 再关闭持有长连接的适配器运行时（鸭子类型，不感知具体 Provider）
        await app.state.quote_stream_service.shutdown()
        pending = []
        for record in app.state.registry.list_records():
            shutdown = getattr(record.adapter, "shutdown_runtime", None)
            if shutdown is None:
                continue
            result = shutdown()
            if inspect.isawaitable(result):
                # 钩子返回的停机任务必须 await 到完成：裸 create_task 无人
                # await 会在 uvicorn 退出时被取消半途（ws.close 不落）、
                # TestClient portal 下直接销毁
                pending.append(result)
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning(
                        "adapter shutdown hook failed: %r", result, exc_info=result
                    )

    app = FastAPI(
        title="tapehub",
        version=settings.version,
        description="通用行情数据网关（历史 K 线查询；tqsdk 实时快照与 SSE 流式）",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.capability_service = CapabilityService(registry)
    app.state.instrument_resolve_service = InstrumentResolveService()
    router = ProviderRouter(registry)
    app.state.bar_query_service = BarQueryService(
        router, upstream_timeout_seconds=settings.upstream_timeout_seconds
    )
    app.state.quote_query_service = QuoteQueryService(
        router, upstream_timeout_seconds=settings.upstream_timeout_seconds
    )
    app.state.quote_stream_service = QuoteStreamService(
        router,
        max_clients=settings.stream_max_clients,
        heartbeat_seconds=settings.stream_heartbeat_seconds,
        idle_timeout_seconds=settings.stream_idle_timeout_seconds,
    )

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(instruments.router)
    app.include_router(market.router)
    app.include_router(quotes.router)
    return app


app = create_app()
