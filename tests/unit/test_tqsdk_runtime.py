"""TqSdkRuntime 单元测试（离线：注入假 SDK 模块，零网络零凭据）。

假 SDK（``tests/fixtures/providers/fake_tqsdk_runtime``）模拟 tqsdk 的同步
驱动模型（Runtime 线程 tick 驱动），脚本旋钮与语义见该模块 docstring。

覆盖：生命周期与并发取数、并发闸门、watchdog 新一代重建、跨交易日回收、
致命构造失败、停机时在飞请求显式失败（不挂起调用方）；
实时快照订阅：首包发布、is_changing 增量刷新、引用计数与退订、
休眠唤醒复用（不重复 get_quote）、休眠订阅回收阈值重建与活跃重订；
实时快照流监听器：subscribed/quote 事件、代际回收 recycling 与
重订恢复、适配器 stream_quote 链路与 coalesce。
"""

import asyncio
import concurrent.futures
import time
import types

import pytest

from tapehub.domain.errors import ConcurrencyLimitError, QuoteNotReadyError, UpstreamError
from tapehub.providers.tqsdk.runtime import RuntimeStatus, TqSdkRuntime
from tests.fixtures.providers import fake_tqsdk_runtime as fake_rt
from tests.fixtures.providers.fake_tqsdk_runtime import FakeTqApi


@pytest.fixture()
def fake_tqsdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    return fake_rt.install(monkeypatch)


def _make_runtime(**overrides: object) -> TqSdkRuntime:
    kwargs: dict[str, object] = {
        "auth_user": "user@example.com",
        "auth_password": "secret",
        "max_inflight": 4,
        "tick_seconds": 0.01,
        "construct_timeout": 10.0,
    }
    kwargs.update(overrides)
    return TqSdkRuntime(**kwargs)  # type: ignore[arg-type]


async def _fetch(runtime: TqSdkRuntime, symbol: str) -> list[dict[str, object]]:
    cf = runtime.fetch({"symbol": symbol, "duration_seconds": 86400, "data_length": 3})
    return await asyncio.wrap_future(cf)


def test_lifecycle_concurrent_fetches_and_shutdown(fake_tqsdk: types.ModuleType) -> None:
    runtime = _make_runtime()
    runtime.start()
    assert runtime.status == RuntimeStatus.READY
    assert runtime.generation == 1

    asyncio.run(_gather_and_check(runtime))
    runtime.shutdown()
    assert FakeTqApi.instances
    assert all(inst.closed for inst in FakeTqApi.instances)


async def _gather_and_check(runtime: TqSdkRuntime) -> None:
    symbols = ["SHFE.au2612", "SHFE.cu2601", "SSE.600000"]
    rows_list = await asyncio.gather(*[_fetch(runtime, s) for s in symbols])
    for rows in rows_list:
        assert len(rows) == FakeTqApi.rows_per_call


def test_inflight_gate_rejects_beyond_limit(fake_tqsdk: types.ModuleType) -> None:
    fake_rt.FETCH_DELAY = 0.5  # 制造慢请求：在飞窗口覆盖整个场景
    runtime = _make_runtime(max_inflight=2)
    runtime.start()

    async def scenario() -> None:
        slow = [
            runtime.fetch({"symbol": "A", "duration_seconds": 60, "data_length": 1})
            for _ in range(2)
        ]
        # 等两笔慢取数真正派发并进入在飞 await（get_kline_serial 已调用）——
        # 闸门语义必须覆盖「在飞取数」本身，而非仅入队未派发窗口
        api = FakeTqApi.instances[-1]
        deadline = time.time() + 5
        while api._inflight_fetches < 2 and time.time() < deadline:
            await asyncio.sleep(0.01)
        assert api._inflight_fetches >= 2, "slow fetches never entered in-flight state"
        # 两笔在飞未完成时，第三笔拒绝
        with pytest.raises(ConcurrencyLimitError):
            runtime.fetch({"symbol": "C", "duration_seconds": 60, "data_length": 1})
        # 放行后闸门恢复
        await asyncio.gather(*[asyncio.wrap_future(cf) for cf in slow])
        cf = runtime.fetch({"symbol": "D", "duration_seconds": 60, "data_length": 1})
        await asyncio.wrap_future(cf)

    asyncio.run(scenario())
    runtime.shutdown()


def test_inflight_peak_bounded_by_limit(fake_tqsdk: types.ModuleType) -> None:
    """超限提交被拒且在飞峰值不超上限（稳态并发保护，非仅入队窗口）。

    ``fetch`` 在闸门处同步抛 ``ConcurrencyLimitError``（慢取数占满 3 个
    在飞名额期间，后续提交全部被拒）。
    """
    fake_rt.FETCH_DELAY = 0.3
    runtime = _make_runtime(max_inflight=3)
    runtime.start()

    async def scenario() -> None:
        cfs: list[concurrent.futures.Future] = []
        rejected_sync = 0
        for i in range(10):
            try:
                cfs.append(
                    runtime.fetch({"symbol": f"S{i}", "duration_seconds": 60, "data_length": 1})
                )
            except ConcurrencyLimitError:
                rejected_sync += 1
        assert rejected_sync >= 1, "over-limit submissions must be rejected"
        results = await asyncio.gather(
            *[asyncio.wrap_future(cf) for cf in cfs], return_exceptions=True
        )
        assert all(not isinstance(r, BaseException) for r in results)

    asyncio.run(scenario())
    runtime.shutdown()
    assert FakeTqApi.instances[-1].inflight_peak <= 3


def test_watchdog_rebuilds_new_generation(fake_tqsdk: types.ModuleType) -> None:
    FakeTqApi.fail_wait_update_times = 5  # 连续故障触发 watchdog 重建
    runtime = _make_runtime(tick_seconds=0.02)
    runtime.start()

    async def scenario() -> None:
        # 重建窗口内，请求被显式失败（UpstreamError / 闸门拒绝）属预期行为；
        # 代际推进与失败计数消费存在时序竞态，用重试对齐而非精确断言
        try:
            await _fetch(runtime, "SHFE.au2612")
        except (UpstreamError, ConcurrencyLimitError):
            pass
        deadline = time.time() + 12
        while runtime.generation < 2 and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert runtime.generation >= 2, (
            f"watchdog rebuild did not advance generation: "
            f"gen={runtime.generation} status={runtime.status} "
            f"inflight unknown last_error={runtime.last_error}"
        )
        # 新一代可正常取数（重建窗口残留可能短暂拒绝，重试对齐）
        last: Exception | None = None
        for _ in range(10):
            try:
                await _fetch(runtime, "SHFE.cu2601")
                last = None
                break
            except (UpstreamError, ConcurrencyLimitError) as exc:
                last = exc
                await asyncio.sleep(0.2)
        assert last is None, f"fetch never succeeded after rebuild: {last}"

    asyncio.run(scenario())
    runtime.shutdown()


def test_recycle_on_cross_trading_day(
    fake_tqsdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _make_runtime()
    # 注入：下一次回收时刻立即到达
    monkeypatch.setattr(runtime, "_next_recycle_ts", lambda: time.time() + 0.2, raising=False)
    runtime.start()
    asyncio.run(_fetch(runtime, "SHFE.au2612"))
    gen_before = runtime.generation
    deadline = time.time() + 5
    while runtime.generation == gen_before and time.time() < deadline:
        time.sleep(0.05)
    assert runtime.generation == gen_before + 1  # 跨交易日回收重建了新一代
    asyncio.run(_fetch(runtime, "SHFE.cu2601"))  # 新一代可正常取数
    runtime.shutdown()


def test_fatal_construct_rejects_and_fails_jobs(fake_tqsdk: types.ModuleType) -> None:
    FakeTqApi.fail_construct = True
    runtime = _make_runtime()
    runtime.start()
    assert runtime.status == RuntimeStatus.FATAL
    with pytest.raises(UpstreamError):
        runtime.fetch({"symbol": "X", "duration_seconds": 60, "data_length": 1})
    runtime.shutdown()


def test_shutdown_fails_pending_fetches(fake_tqsdk: types.ModuleType) -> None:
    fake_rt.FETCH_DELAY = 5.0  # 派发一笔必然来不及完成的慢请求
    runtime = _make_runtime()
    runtime.start()
    cf = runtime.fetch({"symbol": "SLOW", "duration_seconds": 60, "data_length": 1})
    runtime.shutdown(timeout=10)
    assert cf.done()  # 在飞请求被显式失败，而不是永远挂起
    assert isinstance(cf.exception(), UpstreamError)


# ---------------------------------------------------------------------------
# 实时快照订阅
# ---------------------------------------------------------------------------


def _wait_latest(runtime: TqSdkRuntime, symbol: str, timeout: float = 5.0) -> dict:
    """轮询等待快照出现（模拟适配器等首包语义）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = runtime.latest(symbol)
        if snapshot is not None:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"snapshot for {symbol!r} never appeared")


def test_quote_subscribe_publishes_first_snapshot(fake_tqsdk: types.ModuleType) -> None:
    runtime = _make_runtime()
    runtime.start()
    assert runtime.latest("SHFE.au2612") is None  # 未订阅：无快照

    runtime.subscribe("SHFE.au2612")
    snapshot = _wait_latest(runtime, "SHFE.au2612")
    assert snapshot["datetime"] == "2026-11-04 10:14:46.500000"
    assert snapshot["last_price"] == 848.24
    assert FakeTqApi.instances[0].get_quote_calls == ["SHFE.au2612"]
    runtime.shutdown()


def test_quote_snapshot_refreshed_on_updates(fake_tqsdk: types.ModuleType) -> None:
    FakeTqApi.quote_update_every_ticks = 3  # 每 3 个 tick 推送一次增量
    runtime = _make_runtime()
    runtime.start()
    runtime.subscribe("SHFE.au2612")
    first = _wait_latest(runtime, "SHFE.au2612")
    assert first["last_price"] == 848.24

    deadline = time.time() + 5
    latest = first
    while latest["last_price"] == 848.24 and time.time() < deadline:
        time.sleep(0.01)
        latest = runtime.latest("SHFE.au2612")  # type: ignore[assignment]
    assert latest["last_price"] > 848.24  # is_changing 增量已刷新缓存
    runtime.shutdown()


def test_quote_refcount_and_unsubscribe(fake_tqsdk: types.ModuleType) -> None:
    runtime = _make_runtime()
    runtime.start()
    symbol = "SHFE.au2612"
    runtime.subscribe(symbol)
    runtime.subscribe(symbol)  # 第二个订阅者
    _wait_latest(runtime, symbol)

    runtime.unsubscribe(symbol)  # 还剩一个订阅者：快照保留
    assert runtime.latest(symbol) is not None
    runtime.unsubscribe(symbol)  # 最后一个订阅者退订：停止维护
    assert runtime.latest(symbol) is None
    runtime.shutdown()


def test_quote_dormant_wakeup_reuses_subscription(fake_tqsdk: types.ModuleType) -> None:
    runtime = _make_runtime()
    runtime.start()
    symbol = "SHFE.au2612"
    runtime.subscribe(symbol)
    _wait_latest(runtime, symbol)
    runtime.unsubscribe(symbol)  # 转休眠（上游订阅仍在）

    calls_before = list(FakeTqApi.instances[0].get_quote_calls)
    runtime.subscribe(symbol)  # 休眠唤醒：不再次 get_quote
    snapshot = _wait_latest(runtime, symbol)
    assert snapshot["last_price"] == 848.24
    assert FakeTqApi.instances[0].get_quote_calls == calls_before
    runtime.shutdown()


def test_dormant_subscription_recycle_rebuilds_generation(
    fake_tqsdk: types.ModuleType,
) -> None:
    runtime = _make_runtime(subscription_limit=2)
    runtime.start()
    # 常驻订阅：回收后应在新一代自动重订
    runtime.subscribe("SSE.600000")
    _wait_latest(runtime, "SSE.600000")

    for symbol in ("SHFE.au2612", "SSE.510050"):  # 两个休眠订阅达到阈值
        runtime.subscribe(symbol)
        _wait_latest(runtime, symbol)
        runtime.unsubscribe(symbol)
    assert runtime._dormant_subscription_count() == 2  # type: ignore[attr-defined]

    deadline = time.time() + 10
    while runtime.generation < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert runtime.generation >= 2, "dormant subscription recycle did not rebuild"
    assert runtime._dormant_subscription_count() == 0  # type: ignore[attr-defined]

    snapshot = _wait_latest(runtime, "SSE.600000")  # 活跃订阅在新一代重订
    assert snapshot["datetime"] != ""
    runtime.shutdown()


def test_quote_subscribe_rejected_when_fatal(fake_tqsdk: types.ModuleType) -> None:
    FakeTqApi.fail_construct = True
    runtime = _make_runtime()
    runtime.start()
    with pytest.raises(UpstreamError):
        runtime.subscribe("SHFE.au2612")
    runtime.shutdown()


# ---------------------------------------------------------------------------
# 适配器 shared 模式链路（经同一假 SDK，验证与 per_request 语义一致）
# ---------------------------------------------------------------------------


def test_adapter_shared_mode_full_chain(
    fake_tqsdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from tapehub.domain.intervals import Interval
    from tapehub.domain.models import Adjust, AssetType, BarQuery, DataVariant, Instrument
    from tapehub.providers.manifest import load_builtin_manifest
    from tapehub.providers.registry import build_provider_record
    from tapehub.providers.tqsdk.adapter import ProviderAdapter
    from tests.conftest import make_settings

    manifest = load_builtin_manifest("tqsdk")
    settings = make_settings(
        tqsdk_conn_mode="shared",
        tqsdk_auth_user="u@example.com",
        tqsdk_auth_password="secret",
    )
    adapter = build_provider_record("tqsdk", manifest, ProviderAdapter, settings=settings).adapter

    cn = ZoneInfo("Asia/Shanghai")
    query = BarQuery(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-AU2612"),
        interval=Interval.parse("P1D"),
        start=datetime(2026, 1, 1, tzinfo=cn),
        end=datetime(2026, 1, 10, tzinfo=cn),
        adjust=Adjust.NONE,
        variant=DataVariant.TQSDK,
        offset=0,
        limit=500,
    )
    batch = asyncio.run(adapter.fetch_bars(query))

    assert batch.provider == "tqsdk"
    assert batch.count == FakeTqApi.rows_per_call
    assert batch.partial is False
    # 行时间戳为北京时间口径的窗口内数据
    assert query.start <= batch.bars[0].timestamp <= query.end
    # shared 模式：长连接实例复用（一次构造），且已带完整上游参数
    assert len(FakeTqApi.instances) == 1
    assert FakeTqApi.instances[0].auth.user_name == "u@example.com"

    # 停机钩子：lifespan 调用后实例关闭
    adapter.shutdown_runtime()
    assert FakeTqApi.instances[0].closed


# ---------------------------------------------------------------------------
# 适配器实时快照链路（经同一假 SDK）
# ---------------------------------------------------------------------------


def _quote_query(**overrides: object):  # type: ignore[no-untyped-def]
    from tapehub.domain.models import AssetType, DataVariant, Instrument, QuoteQuery

    base: dict[str, object] = {
        "asset_type": AssetType.CONTRACT,
        "instrument": Instrument.parse("CN-SHFE-AU2612"),
        "variant": DataVariant.TQSDK,
    }
    base.update(overrides)
    return QuoteQuery(**base)  # type: ignore[arg-type]


def _quote_adapter(**setting_overrides: object):  # type: ignore[no-untyped-def]
    from tapehub.providers.manifest import load_builtin_manifest
    from tapehub.providers.registry import build_provider_record
    from tapehub.providers.tqsdk.adapter import ProviderAdapter
    from tests.conftest import make_settings

    manifest = load_builtin_manifest("tqsdk")
    options: dict[str, object] = {
        "tqsdk_conn_mode": "shared",
        "tqsdk_auth_user": "u@example.com",
        "tqsdk_auth_password": "secret",
    }
    options.update(setting_overrides)
    settings = make_settings(**options)
    record = build_provider_record("tqsdk", manifest, ProviderAdapter, settings=settings)
    adapter = record.adapter
    assert adapter is not None
    return adapter


def test_adapter_shared_mode_quote_chain(fake_tqsdk: types.ModuleType) -> None:
    adapter = _quote_adapter()
    query = _quote_query()
    quote = asyncio.run(adapter.fetch_quote(query))

    assert quote.provider == "tqsdk"
    assert quote.last_price == 848.24
    assert quote.bid_price1 == 848.26
    assert quote.ask_price5 == 848.6
    assert quote.bid_volume5 == 1
    assert quote.volume == 200250.0
    assert quote.amount == 169906871060.0
    # 快照行情时间为北京时间（+08:00）
    assert quote.timestamp.isoformat().startswith("2026-11-04T10:14:46.500000+08:00")
    # extensions 白名单（P5 实证字段）
    assert quote.extensions is not None
    assert quote.extensions["open_interest"] == 160306.0
    assert quote.extensions["upper_limit"] == 964.56

    # 第二次请求读快照缓存：同一 Runtime 实例，不再新建 TqApi
    quote2 = asyncio.run(adapter.fetch_quote(query))
    assert quote2.last_price == 848.24
    assert len(FakeTqApi.instances) == 1
    adapter.shutdown_runtime()
    assert FakeTqApi.instances[0].closed


def test_adapter_quote_not_ready_timeout(fake_tqsdk: types.ModuleType) -> None:
    FakeTqApi.quote_first_packet_ticks = 100_000  # 首包永不到达
    adapter = _quote_adapter(tqsdk_first_snapshot_timeout=0.3)
    with pytest.raises(QuoteNotReadyError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert excinfo.value.details["timeout_s"] == 0.3
    assert excinfo.value.details["symbol"] == "SHFE.au2612"
    adapter.shutdown_runtime()


def test_adapter_quote_per_request_path(fake_tqsdk: types.ModuleType) -> None:
    """per_request（回滚模式）快照：独立实例订阅取首包后关闭。"""
    adapter = _quote_adapter(tqsdk_conn_mode="per_request")
    quote = asyncio.run(adapter.fetch_quote(_quote_query()))
    assert quote.last_price == 848.24
    assert len(FakeTqApi.instances) == 1
    assert FakeTqApi.instances[0].closed
    # per_request 模式不持有 Runtime 常驻
    adapter.shutdown_runtime()


# ---------------------------------------------------------------------------
# runtime_status 回填（供 /v1/capabilities 鸭子类型读取）
# ---------------------------------------------------------------------------


def test_adapter_runtime_status_lifecycle(fake_tqsdk: types.ModuleType) -> None:
    """shared 模式 runtime_status 随 Runtime 生命周期变化（idle → ready）。"""
    adapter = _quote_adapter()
    assert adapter.runtime_status == "idle"  # 懒启动前

    quote = asyncio.run(adapter.fetch_quote(_quote_query()))
    assert quote.last_price == 848.24
    assert adapter.runtime_status == "ready"
    adapter.shutdown_runtime()


def test_adapter_runtime_status_fatal(fake_tqsdk: types.ModuleType) -> None:
    """构造致命失败：Runtime 进入 fatal，runtime_status 如实透出。"""
    FakeTqApi.fail_construct = True
    adapter = _quote_adapter(tqsdk_first_snapshot_timeout=0.2)
    assert adapter.runtime_status == "idle"
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert adapter.runtime_status == "fatal"
    adapter.shutdown_runtime()


def test_adapter_runtime_status_per_request_is_none(fake_tqsdk: types.ModuleType) -> None:
    """per_request 模式无常驻长连接：runtime_status 为 None（目录回退 unknown）。"""
    adapter = _quote_adapter(tqsdk_conn_mode="per_request")
    assert adapter.runtime_status is None


# ---------------------------------------------------------------------------
# 实时快照流监听器
# ---------------------------------------------------------------------------


def _wait_events(
    received: list[tuple[str, str, dict | None]], kinds: tuple[str, ...], timeout: float = 8.0
) -> None:
    """轮询等待指定种类的事件**按次数**到达（允许穿插其他事件）。"""
    from collections import Counter

    want = Counter(kinds)
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = Counter(kind for kind, _sym, _snap in received)
        if all(got[kind] >= count for kind, count in want.items()):
            return
        time.sleep(0.02)
    got = Counter(kind for kind, _sym, _snap in received)
    raise AssertionError(f"expected events {dict(want)}, got {dict(got)}")


def test_stream_listener_subscribed_and_quote_events(fake_tqsdk: types.ModuleType) -> None:
    FakeTqApi.quote_update_every_ticks = 3  # 每 3 tick 推一次增量
    runtime = _make_runtime()
    runtime.start()
    received: list[tuple[str, str, dict | None]] = []
    remove = runtime.add_stream_listener(
        "SHFE.au2612", lambda kind, sym, snap: received.append((kind, sym, snap))
    )
    runtime.subscribe("SHFE.au2612")

    _wait_events(received, ("subscribed", "quote"))
    subscribed = next(item for item in received if item[0] == "subscribed")
    assert subscribed[2] is not None and subscribed[2]["last_price"] == 848.24
    # 增量最终到达（首包 tick 会同时触发一帧同值 quote，取最新即可）
    deadline = time.time() + 8
    latest = subscribed[2]
    while latest["last_price"] == 848.24 and time.time() < deadline:
        time.sleep(0.02)
        quotes = [snap for kind, _s, snap in received if kind == "quote" and snap]
        latest = quotes[-1]
    assert latest["last_price"] > 848.24

    remove()
    runtime.shutdown()


def test_stream_listener_recycle_emits_events_and_resubscribes(
    fake_tqsdk: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """代际回收：recycling 事件 → 新一代自动重订 → subscribed 恢复。"""
    runtime = _make_runtime()
    monkeypatch.setattr(runtime, "_next_recycle_ts", lambda: time.time() + 0.3)
    runtime.start()
    received: list[tuple[str, str, dict | None]] = []
    runtime.add_stream_listener(
        "SHFE.au2612", lambda kind, sym, snap: received.append((kind, sym, snap))
    )
    runtime.subscribe("SHFE.au2612")
    _wait_events(received, ("subscribed",))

    _wait_events(received, ("recycling", "subscribed", "subscribed"), timeout=10)
    assert runtime.generation >= 2
    runtime.shutdown()


def test_stream_listener_upstream_disconnected_on_wait_failure(
    fake_tqsdk: types.ModuleType,
) -> None:
    runtime = _make_runtime()
    runtime.start()
    received: list[tuple[str, str, dict | None]] = []
    runtime.add_stream_listener(
        "SHFE.au2612", lambda kind, sym, snap: received.append((kind, sym, snap))
    )
    runtime.subscribe("SHFE.au2612")
    _wait_events(received, ("subscribed",))
    FakeTqApi.fail_wait_update_times = 1  # 首包后注入单次异常（不触发 watchdog）
    _wait_events(received, ("upstream_disconnected",))
    runtime.shutdown()


def test_stream_listener_notified_on_shutdown(fake_tqsdk: types.ModuleType) -> None:
    runtime = _make_runtime()
    runtime.start()
    received: list[tuple[str, str, dict | None]] = []
    runtime.add_stream_listener(
        "SHFE.au2612", lambda kind, sym, snap: received.append((kind, sym, snap))
    )
    runtime.subscribe("SHFE.au2612")
    _wait_events(received, ("subscribed",))
    runtime.shutdown()
    _wait_events(received, ("upstream_disconnected",))


def test_adapter_stream_quote_chain(fake_tqsdk: types.ModuleType) -> None:
    """适配器流链路：subscribed 首事件 → quote 帧；关闭即退订。"""
    FakeTqApi.quote_update_every_ticks = 2
    adapter = _quote_adapter()

    async def scenario() -> list:
        agen = adapter.stream_quote(_quote_query())
        events = []
        for _ in range(5):
            events.append(await asyncio.wait_for(agen.__anext__(), 5))
        await agen.aclose()
        return events

    events = asyncio.run(scenario())
    assert events[0].status is not None and events[0].status.value == "subscribed"
    assert events[1].quote is not None and events[1].quote.last_price == 848.24
    assert events[1].quote.timestamp.isoformat().endswith("+08:00")
    # 后续 quote 帧持续推进（首包 tick 会紧跟一帧同值 quote，取到增量即通过）
    prices = [event.quote.last_price for event in events if event.quote is not None]
    assert any(price > 848.24 for price in prices)

    runtime = adapter._runtime  # type: ignore[attr-defined]
    assert "SHFE.au2612" not in runtime._sub_refs  # 关闭即退订（引用归零）
    adapter.shutdown_runtime()


def test_adapter_stream_quote_coalesce_keeps_latest(fake_tqsdk: types.ModuleType) -> None:
    """有界队列丢旧保新：慢消费者拿到的应是最新价（跳过中间帧）。"""
    FakeTqApi.quote_update_every_ticks = 1  # 每 tick 推增量（tick≈50ms）
    adapter = _quote_adapter(stream_queue_size=1)

    async def scenario() -> tuple:
        agen = adapter.stream_quote(_quote_query())
        first_status = await asyncio.wait_for(agen.__anext__(), 5)
        first_quote = await asyncio.wait_for(agen.__anext__(), 5)
        await asyncio.sleep(0.8)  # 慢消费期间增量大量入队，队列只留最新
        latest_quote = await asyncio.wait_for(agen.__anext__(), 5)
        await agen.aclose()
        return first_status, first_quote, latest_quote

    first_status, first_quote, latest_quote = asyncio.run(scenario())
    assert first_status.status is not None
    assert first_quote.quote is not None
    assert latest_quote.quote is not None
    # 0.8s 内增量多次入队又被丢旧：coalesce 后消费者直接看到跳增的价
    assert latest_quote.quote.last_price - first_quote.quote.last_price >= 2
    adapter.shutdown_runtime()


def test_adapter_stream_quote_requires_shared_mode(fake_tqsdk: types.ModuleType) -> None:
    from tapehub.domain.errors import UnsupportedCapabilityError

    adapter = _quote_adapter(tqsdk_conn_mode="per_request")
    # 受理校验在调用时急切执行（响应头发出前统一 JSON 错误）
    with pytest.raises(UnsupportedCapabilityError):
        adapter.stream_quote(_quote_query())
    adapter.shutdown_runtime()
