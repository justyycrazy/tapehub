"""应用 lifespan 停机钩子协议测试。

覆盖停机钩子协议：停机钩子返回的 awaitable（如 twelvedata 的
异步 WS 停机任务）必须被 lifespan await 到完成——裸 create_task 无人
await 会在 uvicorn 退出时被取消半途、TestClient portal 下直接销毁，
ws.close 永不落。
"""

import asyncio

from fastapi.testclient import TestClient

from tapehub.api.app import create_app
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tests.conftest import fake_routing, make_settings
from tests.fixtures.providers.fake_provider import FakeBarProvider


class _AwaitableShutdownProvider(FakeBarProvider):
    """停机钩子返回异步任务的 Fake 适配器（twelvedata 式协议）。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.shutdown_done = False

    def shutdown_runtime(self) -> object:
        async def _run() -> None:
            await asyncio.sleep(0.01)
            self.shutdown_done = True

        return asyncio.get_running_loop().create_task(_run())


def _registry_with_awaitable_hook() -> tuple[ProviderRegistry, _AwaitableShutdownProvider]:
    from pathlib import Path

    manifest_path = Path(__file__).parents[1] / "fixtures" / "manifests" / "fake_provider.yaml"
    from tapehub.providers.manifest import load_manifest_file

    manifest = load_manifest_file(manifest_path)
    record = build_provider_record("fake-provider", manifest, _AwaitableShutdownProvider)
    adapter = record.adapter
    assert isinstance(adapter, _AwaitableShutdownProvider)
    return ProviderRegistry([record], fake_routing()), adapter


def test_lifespan_awaits_awaitable_shutdown_hook() -> None:
    """with-block 退出（lifespan shutdown）后，钩子返回的任务已执行到完成。"""
    registry, adapter = _registry_with_awaitable_hook()
    app = create_app(settings=make_settings(), registry=registry)

    with TestClient(app):
        assert adapter.shutdown_done is False  # 运行期不触发停机

    assert adapter.shutdown_done is True


def test_lifespan_collects_multiple_hook_tasks() -> None:
    """多个 Provider 的可等待停机钩子并行收尾，全部完成才退出。"""
    from pathlib import Path

    from tapehub.providers.manifest import load_manifest_file

    manifest_path = Path(__file__).parents[1] / "fixtures" / "manifests" / "fake_provider.yaml"
    manifest = load_manifest_file(manifest_path)
    r1 = build_provider_record("fake-provider-a", manifest, _AwaitableShutdownProvider)
    r2 = build_provider_record("fake-provider-b", manifest, _AwaitableShutdownProvider)
    a1 = r1.adapter
    a2 = r2.adapter
    assert isinstance(a1, _AwaitableShutdownProvider) and isinstance(
        a2, _AwaitableShutdownProvider
    )
    routing = {}
    for record in (r1, r2):
        for cap in record.capabilities:
            for market in cap.markets:
                for asset_type in cap.asset_types:
                    routing[(cap.data_type, market, asset_type)] = (record.name,)
    registry = ProviderRegistry([r1, r2], routing)
    app = create_app(settings=make_settings(), registry=registry)

    with TestClient(app):
        pass

    assert a1.shutdown_done and a2.shutdown_done


def test_lifespan_applies_thread_pool_size() -> None:
    """lifespan 启动把 Settings.thread_pool_size 应用到 anyio 默认 limiter。

    limiter 是 per-event-loop 的 RunVar：测试在同一 loop 内直接驱动
    lifespan 协程（不经 TestClient portal，其 loop 与断言所在的 loop 不同），
    结束前还原原值避免污染同 loop 内的其他测试。
    """
    import anyio.to_thread

    async def scenario() -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        try:
            app = create_app(settings=make_settings(thread_pool_size=13))
            async with app.router.lifespan_context(app):
                assert limiter.total_tokens == 13
        finally:
            limiter.total_tokens = original

    asyncio.run(scenario())
