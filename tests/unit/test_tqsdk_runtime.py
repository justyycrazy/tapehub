"""TqSdkRuntime 单元测试（离线：注入假 SDK 模块，零网络零凭据）。

假 SDK 模拟 tqsdk 的同步驱动模型（Runtime 线程 tick 驱动）：

- ``create_task`` 把协程挂到私有循环；
- ``get_kline_serial`` 返回可 await 的序列对象（任务推进若干 tick 后完成）；
- ``get_quote`` 返回可 await 的 dict-like Quote（推进若干 tick 收到首包；
  循环未运行时同步驱动至首包，模拟真实 SDK 的阻塞语义）；
- ``wait_update(deadline)`` 推进私有循环一小段，按脚本推进行情首包/增量
  （``_changed_ids`` 供 ``is_changing`` 判定），可注入连续故障（watchdog）。

覆盖：生命周期与并发取数、并发闸门、watchdog 新一代重建、跨交易日回收、
致命构造失败、停机时在飞请求显式失败（不挂起调用方）；
实时快照订阅：首包发布、is_changing 增量刷新、引用计数与退订、
休眠唤醒复用（不重复 get_quote）、休眠订阅回收阈值重建与活跃重订。
"""

import asyncio
import sys
import time
import types

import pytest

from tapehub.domain.errors import ConcurrencyLimitError, QuoteNotReadyError, UpstreamError
from tapehub.providers.tqsdk.runtime import RuntimeStatus, TqSdkRuntime

# 取数协程推进所需时长（秒）；闸门/停机测试通过调大它制造慢请求
FETCH_DELAY = 0.0
TICKS_PER_FETCH = 2


def _quote_fields(last_price: float) -> dict[str, object]:
    """首包快照字段（取自 P5 探针 20261104T012958Z 的 au2612 实测样本）。"""
    return {
        "datetime": "2026-11-04 10:14:46.500000",
        "last_price": last_price,
        "ask_price1": 848.3, "ask_volume1": 2,
        "bid_price1": 848.26, "bid_volume1": 2,
        "ask_price2": 848.36, "ask_volume2": 8,
        "bid_price2": 848.24, "bid_volume2": 12,
        "ask_price3": 848.42, "ask_volume3": 4,
        "bid_price3": 848.22, "bid_volume3": 1,
        "ask_price4": 848.58, "ask_volume4": 4,
        "bid_price4": 848.18, "bid_volume4": 1,
        "ask_price5": 848.6, "ask_volume5": 3,
        "bid_price5": 848.16, "bid_volume5": 1,
        "highest": 853.64, "lowest": 843.14, "open": 846.0,
        "close": float("nan"), "average": 848.47,
        "volume": 200250, "amount": 169906871060.0,
        "open_interest": 160306, "pre_open_interest": 160467,
        "settlement": float("nan"), "upper_limit": 964.56, "lower_limit": 727.66,
        "pre_settlement": 846.12, "pre_close": 841.8,
    }


class FakeQuote(dict):
    """dict-like Quote：datetime 非空即首包已到；可 await（协程用法）。"""

    def __await__(self):  # type: ignore[no-untyped-def]
        async def _go():  # type: ignore[no-untyped-def]
            while self.get("datetime", "") == "":
                await asyncio.sleep(0.005)
            return self

        return _go().__await__()


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, kind: str) -> list[dict[str, object]]:
        return list(self._rows)


class FakeSerial:
    """可 await 序列：推进 ticks 次（每次需 wait_update 驱动）后返回 frame。"""

    def __init__(self, rows: list[dict[str, object]], ticks: int) -> None:
        self._rows = rows
        self._ticks = ticks

    def __await__(self):  # type: ignore[no-untyped-def]
        async def _go():  # type: ignore[no-untyped-def]
            for _ in range(self._ticks):
                await asyncio.sleep(FETCH_DELAY / max(1, self._ticks))
            return FakeFrame(self._rows)

        return _go().__await__()


class FakeTqApi:
    instances: list["FakeTqApi"] = []
    fail_construct = False
    fail_wait_update_times = 0
    rows_per_call = 3
    # quote 语义脚本：首包在第 N 次 wait_update 到达；其后每 N 次推送一次增量
    quote_first_packet_ticks = 2
    quote_update_every_ticks = 0

    def __init__(
        self, auth: object = None, disable_print: bool = False, loop: object = None, **kw: object
    ) -> None:
        if FakeTqApi.fail_construct:
            raise RuntimeError("auth failed")
        self.auth = auth
        self._loop = (
            loop if isinstance(loop, asyncio.AbstractEventLoop) else asyncio.new_event_loop()
        )
        self._tasks: list[asyncio.Task] = []
        self.closed = False
        self._tick_count = 0
        self._quotes: dict[str, FakeQuote] = {}
        self._quote_first_due: dict[str, int] = {}
        self._changed_ids: set[int] = set()
        self.get_quote_calls: list[str] = []
        FakeTqApi.instances.append(self)

    def create_task(self, coro):  # type: ignore[no-untyped-def]
        task = self._loop.create_task(coro)
        self._tasks.append(task)
        return task

    def get_kline_serial(self, **kwargs: object) -> FakeSerial:
        # datetime 为 ns 时间戳（2026-01-02 00:00 +08:00 起，逐根 +1 日）
        base_ns = 1767254400000000000
        day_ns = 86400 * 1_000_000_000
        rows = [
            {
                "id": i,
                "symbol": kwargs.get("symbol"),
                "datetime": base_ns + i * day_ns,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            }
            for i in range(FakeTqApi.rows_per_call)
        ]
        return FakeSerial(rows, ticks=TICKS_PER_FETCH)

    def get_quote(self, symbol: str) -> FakeQuote:
        """订阅并返回 Quote；循环未运行（同步路径）时驱动至首包（真实 SDK 语义）。"""
        self.get_quote_calls.append(symbol)
        if symbol not in self._quotes:
            quote = FakeQuote({"datetime": "", "last_price": float("nan")})
            self._quotes[symbol] = quote
            self._quote_first_due[symbol] = max(1, FakeTqApi.quote_first_packet_ticks)
        if not self._loop.is_running():
            guard = 0
            while self._quotes[symbol].get("datetime", "") == "":
                self.wait_update()
                guard += 1
                if guard > 1000:  # 防御：脚本配置异常时避免死循环
                    raise RuntimeError("fake get_quote never received first packet")
        return self._quotes[symbol]

    def is_changing(self, obj: object) -> bool:
        return id(obj) in self._changed_ids

    def wait_update(self, deadline: float | None = None) -> bool:
        if FakeTqApi.fail_wait_update_times > 0:
            FakeTqApi.fail_wait_update_times -= 1
            raise RuntimeError("connection broken")
        self._changed_ids = set()
        self._tick_count += 1
        for symbol, quote in self._quotes.items():
            if quote.get("datetime", "") == "":
                due = self._quote_first_due.get(symbol, 0) - 1
                self._quote_first_due[symbol] = due
                if due <= 0:
                    quote.update(_quote_fields(848.24))
                    self._changed_ids.add(id(quote))
            elif (
                FakeTqApi.quote_update_every_ticks > 0
                and self._tick_count % FakeTqApi.quote_update_every_ticks == 0
            ):
                quote["last_price"] = float(quote.get("last_price", 0.0)) + 1.0
                self._changed_ids.add(id(quote))
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            self._loop.run_until_complete(asyncio.wait(pending, timeout=0.05))
        else:
            time.sleep(0.005)
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for task in self._tasks:
            task.cancel()
        try:
            if self._tasks:  # 空 gather 在无当前循环的线程里会取事件循环，需规避
                self._loop.run_until_complete(
                    asyncio.gather(*self._tasks, return_exceptions=True)
                )
        finally:
            self._loop.close()


@pytest.fixture()
def fake_tqsdk(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("tqsdk")

    class FakeTqAuth:
        def __init__(self, user_name: str = "", password: str = "") -> None:
            self.user_name = user_name
            self.password = password

    module.TqApi = FakeTqApi  # type: ignore[attr-defined]
    module.TqAuth = FakeTqAuth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tqsdk", module)
    FakeTqApi.instances = []
    FakeTqApi.fail_construct = False
    FakeTqApi.fail_wait_update_times = 0
    FakeTqApi.quote_first_packet_ticks = 2
    FakeTqApi.quote_update_every_ticks = 0
    global FETCH_DELAY, TICKS_PER_FETCH
    FETCH_DELAY = 0.0
    TICKS_PER_FETCH = 2
    return module


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
    global FETCH_DELAY
    FETCH_DELAY = 0.5  # 制造慢请求，拉宽闸门窗口
    runtime = _make_runtime(max_inflight=2)
    runtime.start()

    async def scenario() -> None:
        slow = [
            runtime.fetch({"symbol": "A", "duration_seconds": 60, "data_length": 1})
            for _ in range(2)
        ]
        # 两笔在飞未完成时，第三笔立即拒绝
        with pytest.raises(ConcurrencyLimitError):
            runtime.fetch({"symbol": "C", "duration_seconds": 60, "data_length": 1})
        # 放行后闸门恢复
        await asyncio.gather(*[asyncio.wrap_future(cf) for cf in slow])
        cf = runtime.fetch({"symbol": "D", "duration_seconds": 60, "data_length": 1})
        await asyncio.wrap_future(cf)

    asyncio.run(scenario())
    runtime.shutdown()


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
    global FETCH_DELAY
    FETCH_DELAY = 5.0  # 派发一笔必然来不及完成的慢请求
    runtime = _make_runtime()
    runtime.start()
    cf = runtime.fetch({"symbol": "SLOW", "duration_seconds": 60, "data_length": 1})
    runtime.shutdown(timeout=10)
    assert cf.done()  # 在飞请求被显式失败，而不是永远挂起
    assert isinstance(cf.exception(), UpstreamError)


# ---------------------------------------------------------------------------
# 实时快照订阅（M2）
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
        instrument=Instrument.parse("CN-SHFE-au2612"),
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
# 适配器实时快照链路（M2，经同一假 SDK）
# ---------------------------------------------------------------------------


def _quote_query(**overrides: object):  # type: ignore[no-untyped-def]
    from tapehub.domain.models import AssetType, DataVariant, Instrument, QuoteQuery

    base: dict[str, object] = {
        "asset_type": AssetType.CONTRACT,
        "instrument": Instrument.parse("CN-SHFE-au2612"),
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
# runtime_status 回填（M4：供 /v1/capabilities 鸭子类型读取）
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
