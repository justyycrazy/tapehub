"""Provider 分组线程池（CapacityLimiter 隔离）单元测试。

机制级覆盖（零网络、零真实凭据）：

- 配置应用：``thread_pool_size_map`` 空 → 维持全局默认池（limiter None，
  行为与历史版本一致）；列出/未列出 → 各自容量；settings 缺省 → None；
  容量 <1 启动期校验失败；
- 快速背压：本家池满同步抛 ``ConcurrencyLimitError``（429 可重试语义，
  details 携带 provider 与 limit），名额释放后恢复正常取数；
- 名额释放语义锚定：abandon 取消后名额立即归还（孤儿线程不占名额），
  显式 limiter 不改变 ``asyncio.wait_for`` 到点超时的 504 路径；
- 分组隔离性证明：单家挂死占满自家池后，他家照常取数（验收核心）；
- 注册表集成：启动期容量落位与诊断日志（分组状态汇总、未知键告警）。
"""

import asyncio
import logging
import threading
import time

import anyio
import pytest
from pydantic import ValidationError

from tapehub.domain.errors import ConcurrencyLimitError
from tapehub.providers.base import (
    DEFAULT_THREAD_POOL_CAPACITY,
    BaseProviderAdapter,
    call_in_thread,
)
from tapehub.providers.manifest import ProviderManifest
from tests.conftest import make_settings


class _StubAdapter(BaseProviderAdapter):
    """最小取数桩：fetch_bars 经 self.call_in_thread 进入（分组）线程池。

    ``sync_fn`` 为线程内执行的同步函数，测试按需替换为挂死/快速实现。
    """

    implemented = True

    def __init__(self, name: str, settings: object = None) -> None:
        super().__init__(
            name=name, manifest=ProviderManifest(provider=name), settings=settings
        )
        self.sync_fn = lambda: f"{self.name}-ok"

    async def fetch_bars(self, query: object) -> object:
        return await self.call_in_thread(self.sync_fn)


# ----------------------------------------------------------------------
# 配置应用（Settings → limiter 生效）
# ----------------------------------------------------------------------


def test_grouping_disabled_by_default_keeps_global_pool() -> None:
    """map 为空（默认）：分组关闭，不持有 limiter（anyio 全局默认池）。"""
    adapter = _StubAdapter("x", make_settings())
    assert adapter.thread_limiter is None


def test_grouping_disabled_without_settings() -> None:
    """settings 缺省：同样分组关闭（历史构造路径不受影响）。"""
    adapter = _StubAdapter("x", None)
    assert adapter.thread_limiter is None


def test_grouped_capacity_listed_and_unlisted() -> None:
    """列出者用配置容量，未列出的内置 Provider 用统一默认容量。"""
    settings = make_settings(thread_pool_size_map={"x": 7})
    listed = _StubAdapter("x", settings)
    unlisted = _StubAdapter("y", settings)
    assert listed.thread_limiter is not None
    assert listed.thread_limiter.total_tokens == 7
    assert unlisted.thread_limiter is not None
    assert unlisted.thread_limiter.total_tokens == DEFAULT_THREAD_POOL_CAPACITY


def test_settings_reject_non_positive_capacity() -> None:
    """容量 <1 属配置错误：pydantic 启动期校验直接失败。"""
    with pytest.raises(ValidationError):
        make_settings(thread_pool_size_map={"x": 0})


# ----------------------------------------------------------------------
# 快速背压（本家池满 → 可重试 429）
# ----------------------------------------------------------------------


def test_full_pool_fast_fails_with_retryable_error() -> None:
    """本家池满：新调用快速抛 ConcurrencyLimitError（不无限排队）；
    名额释放后恢复正常取数。"""
    settings = make_settings(thread_pool_size_map={"a": 1})
    adapter = _StubAdapter("a", settings)
    release = threading.Event()

    def hang() -> str:
        release.wait(5)  # 模拟挂死上游调用（无界 socket 形态）
        return "hung"

    async def scenario() -> None:
        adapter.sync_fn = hang
        task = asyncio.ensure_future(adapter.fetch_bars(None))
        await asyncio.sleep(0.2)  # 挂死调用已进入线程并占用名额
        limiter = adapter.thread_limiter
        assert limiter is not None and limiter.borrowed_tokens == 1

        started = time.perf_counter()
        with pytest.raises(ConcurrencyLimitError) as excinfo:
            await asyncio.wait_for(adapter.fetch_bars(None), 1.0)
        assert time.perf_counter() - started < 0.5  # 快速拒绝
        assert excinfo.value.details["provider"] == "a"
        assert excinfo.value.details["limit"] == 1

        release.set()
        assert await asyncio.wait_for(task, 2.0) == "hung"  # 首个调用完成
        # 名额已归还：恢复正常取数
        adapter.sync_fn = lambda: "a-ok"
        assert await asyncio.wait_for(adapter.fetch_bars(None), 2.0) == "a-ok"

    asyncio.run(scenario())
    release.set()  # 双保险：异常路径不留挂死线程


# ----------------------------------------------------------------------
# 名额释放语义锚定（anyio run_sync 的 async with limiter 包裹语义）
# ----------------------------------------------------------------------


def test_abandon_cancel_releases_limiter_token() -> None:
    """abandon 取消：limiter 名额立即归还（孤儿线程不占名额），
    后续调用即时进入线程。"""

    release = threading.Event()

    def work() -> int:
        release.wait(5)  # 模拟长上游调用
        return 42

    async def scenario() -> None:
        limiter = anyio.CapacityLimiter(1)
        task = asyncio.ensure_future(
            call_in_thread(work, abandon_on_cancel=True, limiter=limiter)
        )
        await asyncio.sleep(0.2)
        assert limiter.borrowed_tokens == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert limiter.borrowed_tokens == 0
        # 名额已归还：后续调用限时即时完成（不被孤儿线程阻塞）
        assert await asyncio.wait_for(call_in_thread(lambda: 7, limiter=limiter), 2.0) == 7

    asyncio.run(scenario())
    release.set()  # 双保险：异常路径不留挂死线程


def test_wait_for_over_limited_call_times_out_at_deadline() -> None:
    """显式 limiter 不改变 wait_for 到点超时语义（应用层 504 路径回归）。

    shield（abandon=False）下取消延迟到线程自超时（0.5s）后送达，
    但调用方在 0.05s deadline 即收到 ``TimeoutError``——与全局池行为一致。
    """

    def work() -> int:
        done = threading.Event()
        done.wait(0.5)  # 自超时返回，主线程不干预
        return 42

    async def scenario() -> None:
        limiter = anyio.CapacityLimiter(1)
        started = time.perf_counter()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(call_in_thread(work, limiter=limiter), 0.05)
        assert time.perf_counter() - started < 0.2  # 到点超时而非等线程

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 分组隔离性证明（验收核心：单家池满不影响他家）
# ----------------------------------------------------------------------


def test_grouping_isolates_starved_provider_from_healthy_one() -> None:
    """隔离性证明：a 家挂死调用占满自家池后，b 家照常取数；
    a 家超额调用快速得到可重试错误——故障爆炸半径收敛到单家。"""
    settings = make_settings(thread_pool_size_map={"a": 2, "b": 2})
    starved = _StubAdapter("a", settings)
    healthy = _StubAdapter("b", settings)
    release = threading.Event()

    def hang() -> str:
        release.wait(5)
        return "hung"

    async def scenario() -> None:
        starved.sync_fn = hang
        t1 = asyncio.ensure_future(starved.fetch_bars(None))
        t2 = asyncio.ensure_future(starved.fetch_bars(None))
        await asyncio.sleep(0.2)  # a 家两个挂死调用已进入线程
        limiter_a = starved.thread_limiter
        assert limiter_a is not None and limiter_a.borrowed_tokens == 2

        # b 家照常取数：限时完成即证明未受 a 家池满影响
        started = time.perf_counter()
        assert await asyncio.wait_for(healthy.fetch_bars(None), 1.0) == "b-ok"
        assert time.perf_counter() - started < 0.5

        # a 家超额：快速可重试拒绝（与 tqsdk 并发闸门同款语义）
        started = time.perf_counter()
        with pytest.raises(ConcurrencyLimitError) as excinfo:
            await asyncio.wait_for(starved.fetch_bars(None), 1.0)
        assert time.perf_counter() - started < 0.5
        assert excinfo.value.details["provider"] == "a"
        assert excinfo.value.details["limit"] == 2

        release.set()
        assert await asyncio.wait_for(t1, 2.0) == "hung"
        assert await asyncio.wait_for(t2, 2.0) == "hung"

    asyncio.run(scenario())
    release.set()  # 双保险：异常路径不留挂死线程


# ----------------------------------------------------------------------
# 注册表集成（启动期配置落位与诊断日志）
# ----------------------------------------------------------------------


def test_registry_applies_grouped_capacities() -> None:
    """注册表构建期把分组容量落到各家适配器（列出者配置值，未列出者默认）。"""
    from tapehub.providers.registry import build_default_registry

    settings = make_settings(thread_pool_size_map={"akshare": 8, "baostock": 2})
    registry = build_default_registry(settings)
    for record in registry.list_records():
        # 适配器模块一律延迟导入 SDK，注册表构建不依赖 SDK 安装
        adapter = record.adapter
        assert adapter is not None
        limiter = adapter.thread_limiter
        assert limiter is not None
        expected = (
            8
            if record.name == "akshare"
            else 2
            if record.name == "baostock"
            else DEFAULT_THREAD_POOL_CAPACITY
        )
        assert limiter.total_tokens == expected


def test_registry_logs_grouping_disabled_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """分组关闭（默认）：启动日志如实声明共用全局池。"""
    from tapehub.providers.registry import build_default_registry

    with caplog.at_level(logging.INFO, logger="tapehub.providers.registry"):
        build_default_registry(make_settings())
    assert any("grouping disabled" in r.getMessage() for r in caplog.records)


def test_registry_warns_unknown_provider_keys(caplog: pytest.LogCaptureFixture) -> None:
    """未知 Provider 名（拼写错误）仅告警不阻断，且不影响已知 Provider
    的容量落位。"""
    from tapehub.providers.registry import build_default_registry

    settings = make_settings(thread_pool_size_map={"akshare": 8, "aksahre": 3})
    with caplog.at_level(logging.WARNING, logger="tapehub.providers.registry"):
        registry = build_default_registry(settings)
    messages = [r.getMessage() for r in caplog.records]
    assert any("unknown provider names" in m and "aksahre" in m for m in messages)
    akshare = registry.get("akshare")
    assert akshare is not None and akshare.adapter is not None
    limiter = akshare.adapter.thread_limiter
    assert limiter is not None and limiter.total_tokens == 8
