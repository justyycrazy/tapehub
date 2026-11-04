"""TqSdkRuntime 单元测试（离线：注入假 SDK 模块，零网络零凭据）。

假 SDK 模拟 tqsdk 的同步驱动模型（Runtime 线程 tick 驱动）：

- ``create_task`` 把协程挂到私有循环；
- ``get_kline_serial`` 返回可 await 的序列对象（任务推进若干 tick 后完成）；
- ``wait_update(deadline)`` 推进私有循环一小段，可注入连续故障（watchdog）。

覆盖：生命周期与并发取数、并发闸门、watchdog 新一代重建、跨交易日回收、
致命构造失败、停机时在飞请求显式失败（不挂起调用方）。
"""

import asyncio
import sys
import time
import types

import pytest

from tapehub.domain.errors import ConcurrencyLimitError, UpstreamError
from tapehub.providers.tqsdk.runtime import RuntimeStatus, TqSdkRuntime

# 取数协程推进所需时长（秒）；闸门/停机测试通过调大它制造慢请求
FETCH_DELAY = 0.0
TICKS_PER_FETCH = 2


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

    def wait_update(self, deadline: float | None = None) -> bool:
        if FakeTqApi.fail_wait_update_times > 0:
            FakeTqApi.fail_wait_update_times -= 1
            raise RuntimeError("connection broken")
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
