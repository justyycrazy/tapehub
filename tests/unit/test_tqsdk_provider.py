"""TqSdk 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime
from typing import Any, ClassVar

import pytest

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    QuoteNotReadyError,
    UnsupportedCapabilityError,
)
from tapehub.domain.intervals import Interval
from tapehub.domain.models import (
    CN_TIMEZONE,
    Adjust,
    AssetType,
    BarQuery,
    DataVariant,
    Instrument,
    QuoteQuery,
)
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tapehub.settings import Settings
from tests.conftest import make_settings
from tests.fixtures.providers.stub_tqsdk import (
    install,
    klines_rows,
    preset_rows,
    sized_klines_rows,
)


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.CONTRACT,
        "instrument": Instrument.parse("CN-SHFE-CU2607"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.NONE,
        "variant": DataVariant.TQSDK,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _adapter(settings: Settings | None = None) -> Any:
    from tapehub.providers.tqsdk.adapter import ProviderAdapter

    manifest = load_builtin_manifest("tqsdk")
    record = build_provider_record(
        "tqsdk",
        manifest,
        ProviderAdapter,
        settings=settings
        or make_settings(
            tqsdk_conn_mode="per_request",
            tqsdk_auth_user="user@qq.com",
            tqsdk_auth_password="secret",
        ),
    )
    return record.adapter


def test_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    StubTqApi = install(monkeypatch)
    preset_rows(monkeypatch, klines_rows(["2026-01-05", "2026-01-06"]))
    adapter = _adapter(
        make_settings(
            tqsdk_conn_mode="per_request",
            tqsdk_auth_user="user@qq.com",
            tqsdk_auth_password="secret",
        )
    )

    batch = asyncio.run(adapter.fetch_bars(_query()))

    api = StubTqApi.instances[-1]
    # TqAuth 凭据经 settings 注入链路传入
    assert api.auth is not None
    assert api.auth.user == "user@qq.com"
    assert api.auth.password == "secret"
    assert api.kwargs.get("disable_print") is True
    assert api.closed is True  # 请求后释放实例

    kwargs = api.calls[0]
    assert kwargs == {
        "symbol": "SHFE.cu2607",
        "duration_seconds": 86400,
        "data_length": 5000,
        "adj_type": None,  # 期货无复权概念，显式传 None
    }

    assert batch.provider == "tqsdk"
    assert batch.count == 2
    bar = batch.bars[0]
    # 桩按 UTC 零点编码日界，归一化输出即北京 08:00
    assert bar.timestamp == datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE)
    assert bar.close == 3005.0
    assert bar.volume == 500.0  # 期货成交量保持手
    assert bar.amount is None  # 上游无成交额字段
    assert bar.extensions is not None
    assert bar.extensions["open_oi"] == 100000.0
    assert bar.extensions["close_oi"] == 101000.0


def test_pre_1970_timestamp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """1970 前负纳秒时间戳映射（纪元算术，Windows fromtimestamp 会崩）。"""
    install(monkeypatch)
    preset_rows(monkeypatch, klines_rows(["1960-06-15", "1960-06-16"]))
    adapter = _adapter(
        make_settings(
            tqsdk_conn_mode="per_request",
            tqsdk_auth_user="user@qq.com",
            tqsdk_auth_password="secret",
        )
    )

    batch = asyncio.run(
        adapter.fetch_bars(
            _query(
                start=datetime(1960, 1, 1, tzinfo=CN_TIMEZONE),
                end=datetime(1961, 1, 1, tzinfo=CN_TIMEZONE),
            )
        )
    )

    assert batch.count == 2
    assert batch.bars[0].timestamp == datetime(1960, 6, 15, 8, tzinfo=CN_TIMEZONE)
    assert batch.bars[1].timestamp == datetime(1960, 6, 16, 8, tzinfo=CN_TIMEZONE)


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("PT1M", 60),
        ("PT5M", 300),
        ("PT15M", 900),
        ("PT30M", 1800),
        ("PT1H", 3600),
        ("P1D", 86400),
    ],
)
def test_duration_seconds(
    interval: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse(interval))))

    from tests.fixtures.providers.stub_tqsdk import StubTqApi

    assert StubTqApi.instances[-1].calls[0]["duration_seconds"] == expected


def test_unsupported_interval_rejected_by_operation_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse("PT2H")))


@pytest.mark.parametrize(
    ("asset_type", "instrument", "expected"),
    [
        # 标准 symbol 恒为大写 4 位年月；交易所原生写法由适配器还原
        #（2026-11-16 第 13 轮期货聚焦轮定案：SHFE/INE/DCE/GFEX 品种小写、
        # CZCE 合约 4→3 位年月码、CFFEX 大写直通）
        (AssetType.CONTRACT, "CN-SHFE-CU2607", "SHFE.cu2607"),
        (AssetType.CONTRACT, "cn-shfe-cu2607", "SHFE.cu2607"),
        (AssetType.CONTRACT, "CN-INE-SC2706", "INE.sc2706"),
        (AssetType.CONTRACT, "CN-DCE-M2705", "DCE.m2705"),
        (AssetType.CONTRACT, "CN-GFEX-LC2706", "GFEX.lc2706"),
        (AssetType.CONTRACT, "CN-CFFEX-IF2706", "CFFEX.IF2706"),
        (AssetType.CONTRACT, "CN-CZCE-RM2609", "CZCE.RM609"),
        # CZCE 主连品种码无年月段，还原表正则天然 no-op 直通
        (AssetType.CONTINUOUS, "CN-CZCE-SR", "KQ.m@CZCE.SR"),
        (AssetType.CONTINUOUS, "CN-SHFE-CU", "KQ.m@SHFE.cu"),
        (AssetType.CONTINUOUS, "CN-DCE-M", "KQ.m@DCE.m"),
        (AssetType.CONTINUOUS, "CN-GFEX-LC", "KQ.m@GFEX.lc"),
        (AssetType.CONTINUOUS, "CN-CFFEX-IF", "KQ.m@CFFEX.IF"),
        (AssetType.WEIGHTED_INDEX, "CN-SHFE-BU", "KQ.i@SHFE.bu"),
        (AssetType.WEIGHTED_INDEX, "CN-INE-SC", "KQ.i@INE.sc"),
        (AssetType.WEIGHTED_INDEX, "CN-CFFEX-IF", "KQ.i@CFFEX.IF"),
        (AssetType.STOCK, "CN-SSE-600000", "SSE.600000"),
        (AssetType.STOCK, "CN-SZSE-000001", "SZSE.000001"),
        (AssetType.INDEX, "CN-SSE-000016", "SSE.000016"),
        (AssetType.ETF, "CN-SSE-510050", "SSE.510050"),
        (AssetType.ETF, "CN-SZSE-159919", "SZSE.159919"),
        # 期权八所（asset_type 三分：commodity-option / index-option /
        # etf-option，2026-11-16 聚焦轮探针定案）：SHFE/INE/DCE/GFEX 品种字母
        # 段小写（全属 commodity-option），SSE/SZSE 数字码（etf-option）与
        # CFFEX（index-option）/CZCE 大写直通
        (AssetType.ETF_OPTION, "CN-SSE-10012437", "SSE.10012437"),
        (AssetType.ETF_OPTION, "CN-SZSE-90008083", "SZSE.90008083"),
        (AssetType.INDEX_OPTION, "CN-CFFEX-IO2612-C-4000", "CFFEX.IO2612-C-4000"),
        (AssetType.COMMODITY_OPTION, "CN-SHFE-CU2701C102000", "SHFE.cu2701C102000"),
        (AssetType.COMMODITY_OPTION, "CN-INE-SC2701C550", "INE.sc2701C550"),
        (AssetType.COMMODITY_OPTION, "CN-DCE-M2701-C-3500", "DCE.m2701-C-3500"),
        (AssetType.COMMODITY_OPTION, "CN-CZCE-SR701C5900", "CZCE.SR701C5900"),
        (AssetType.COMMODITY_OPTION, "CN-GFEX-LC2701-C-90000", "GFEX.lc2701-C-90000"),
    ],
)
def test_kline_symbols(
    asset_type: AssetType,
    instrument: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    query = _query(asset_type=asset_type, instrument=Instrument.parse(instrument))
    asyncio.run(adapter.fetch_bars(query))

    from tests.fixtures.providers.stub_tqsdk import StubTqApi

    assert StubTqApi.instances[-1].calls[0]["symbol"] == expected


def test_missing_user() -> None:
    adapter = _adapter(
        make_settings(tqsdk_conn_mode="per_request", tqsdk_auth_user="", tqsdk_auth_password="pwd")
    )
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["missing"] == ["user"]


def test_missing_password() -> None:
    adapter = _adapter(
        make_settings(
            tqsdk_conn_mode="per_request",
            tqsdk_auth_user="user@qq.com",
            tqsdk_auth_password="",
        )
    )
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["missing"] == ["password"]


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tqsdk", None)  # import 失败，模拟 SDK 未安装
    adapter = _adapter(
        make_settings(
            tqsdk_conn_mode="per_request",
            tqsdk_auth_user="user@qq.com",
            tqsdk_auth_password="secret",
        )
    )
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "not installed" in str(excinfo.value)


def test_closed_interval_filter_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_rows(
        monkeypatch,
        klines_rows(["2025-12-31", "2026-01-02", "2026-01-03", "2026-02-01"]),
    )
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query(offset=1, limit=1)))

    assert batch.count == 1
    assert batch.offset == 1
    assert batch.bars[0].timestamp == datetime(2026, 1, 3, 8, tzinfo=CN_TIMEZONE)


def test_partial_when_window_not_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回数量达上限且最早一根晚于请求 start：更早数据取不到，标记 partial。"""
    install(monkeypatch)
    # 5000 根分钟线，从 2026-01-01T00:00Z 开始（覆盖 start）→ 不算 partial
    adapter = _adapter()
    covered = asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse("PT1M"))))
    assert covered.partial is False

    # 上游只返回 2026-01-06 起的 5000 根，最早一根晚于请求 start（2026-01-05）
    # → start 之前的更早数据取不到，标记 partial
    from tests.fixtures.providers.stub_tqsdk import StubTqApi

    StubTqApi.instances.clear()
    preset_rows(monkeypatch, sized_klines_rows(5000, start_day="2026-01-06"))
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=CN_TIMEZONE),
        end=datetime(2026, 1, 20, tzinfo=CN_TIMEZONE),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    assert batch.partial is True


def test_partial_false_when_bars_cover_start(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_rows(monkeypatch, sized_klines_rows(5000, start_day="2025-12-31"))
    adapter = _adapter()
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=CN_TIMEZONE),
        end=datetime(2026, 1, 20, tzinfo=CN_TIMEZONE),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    # 最早一根早于 start：窗口完整覆盖，即使达到数量上限也不标记 partial
    assert batch.partial is False


def test_adj_type_mapping_for_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """股票复权映射：none→None、qfq→F、hfq→B；adj_type 始终显式传递。"""
    install(monkeypatch)
    adapter = _adapter()
    from tests.fixtures.providers.stub_tqsdk import StubTqApi

    for adjust, expected in ((Adjust.NONE, None), (Adjust.QFQ, "F"), (Adjust.HFQ, "B")):
        query = _query(
            asset_type=AssetType.STOCK,
            instrument=Instrument.parse("CN-SSE-600000"),
            adjust=adjust,
        )
        asyncio.run(adapter.fetch_bars(query))
        assert StubTqApi.instances[-1].calls[0]["adj_type"] == expected


def test_futures_adj_type_always_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """期货无复权概念：adj_type 恒为 None（显式传参，不依赖上游默认值）。"""
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(asset_type=AssetType.CONTRACT)))

    from tests.fixtures.providers.stub_tqsdk import StubTqApi

    assert StubTqApi.instances[-1].calls[0]["adj_type"] is None


# ---------------------------------------------------------------------------
# shared 模式资源生命周期（H1 启动收编 / H2 M2 租约订阅）
# ---------------------------------------------------------------------------


class _StubRuntime:
    """shared Runtime 桩：记录 start/shutdown/订阅账本调用，不发真实上游。"""

    instances: ClassVar[list["_StubRuntime"]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.shutdown_timeouts: list[float] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        _StubRuntime.instances.append(self)

    def start(self) -> None:
        self.started = True

    def shutdown(self, timeout: float = 10.0) -> None:
        self.shutdown_timeouts.append(timeout)

    def fetch(self, kwargs: dict[str, Any]) -> Any:  # pragma: no cover - 本组测试不取数
        raise AssertionError("fetch not expected in lifecycle tests")

    def subscribe(self, symbol: str) -> None:
        self.subscribed.append(symbol)

    def unsubscribe(self, symbol: str) -> None:
        self.unsubscribed.append(symbol)

    def latest(self, symbol: str) -> dict[str, Any] | None:
        return self.snapshots.get(symbol)


def _shared_adapter() -> Any:
    return _adapter(
        make_settings(
            tqsdk_conn_mode="shared",
            tqsdk_auth_user="user@qq.com",
            tqsdk_auth_password="secret",
        )
    )


def _quote(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "asset_type": AssetType.CONTRACT,
        "instrument": Instrument.parse("CN-SHFE-AU2612"),
        "variant": DataVariant.TQSDK,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def _install_shared_stubs(monkeypatch: pytest.MonkeyPatch) -> type[_StubRuntime]:
    import tapehub.providers.tqsdk.adapter as adapter_module

    _StubRuntime.instances.clear()
    monkeypatch.setattr(adapter_module, "TqSdkRuntime", _StubRuntime)
    monkeypatch.setattr(adapter_module, "call_in_thread", _startup_with_checkpoint)
    return _StubRuntime


async def _startup_with_checkpoint(func: Any, *args: Any, **kwargs: Any) -> None:
    """启动完成后留取消检查点：模拟 anyio shield 下「取消在赋值前送达」。"""
    func()  # runtime.start（stub 同步完成）
    await asyncio.sleep(0.05)


def test_ensure_runtime_cancel_collects_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    """启动完成但引用未落账时被取消：非阻塞收编孤儿，重试起全新 Runtime。"""
    stubs = _install_shared_stubs(monkeypatch)
    adapter = _shared_adapter()

    async def scenario() -> None:
        task = asyncio.ensure_future(adapter._ensure_runtime(("u", "p")))
        await asyncio.sleep(0.01)  # 已进入检查点（已启动、未赋值）
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    orphan = stubs.instances[-1]
    assert orphan.started
    assert orphan.shutdown_timeouts == [0]  # 非阻塞收编（join(0) 立即返回）
    assert adapter._runtime is None

    # 重试：起全新 Runtime 并正常落账
    runtime = asyncio.run(adapter._ensure_runtime(("u", "p")))
    assert runtime is stubs.instances[-1] and runtime is not orphan
    assert runtime.shutdown_timeouts == []
    assert adapter._runtime is runtime


def test_quote_lease_released_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """M2 租约（成功路径）：请求结束即退订，refs 归零使回收阀门可达。"""
    _install_shared_stubs(monkeypatch)
    adapter = _shared_adapter()
    runtime = asyncio.run(adapter._ensure_runtime(("u", "p")))
    # 快照桩需带合法 datetime（响应 mapper 要求，空值显式抛错）
    runtime.snapshots["SHFE.au2612"] = {
        "datetime": "2026-11-13 10:00:00.000000",
        "last_price": 100.0,
    }

    asyncio.run(adapter.fetch_quote(_quote()))

    assert runtime.subscribed == ["SHFE.au2612"]
    assert runtime.unsubscribed == ["SHFE.au2612"]  # finally 退订


def test_quote_lease_released_on_first_packet_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2 租约（408 路径）：首包超时仍退订——这是回收阀门可达的关键路径。"""
    _install_shared_stubs(monkeypatch)
    adapter = _shared_adapter()  # 默认 5s 首包超时；stub 永不发布快照

    with pytest.raises(QuoteNotReadyError):
        asyncio.run(adapter.fetch_quote(_quote()))

    runtime = _StubRuntime.instances[-1]
    assert runtime.subscribed == ["SHFE.au2612"]
    assert runtime.unsubscribed == ["SHFE.au2612"]


def test_quote_lease_released_on_client_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2 租约（客户端断开路径）：请求被取消仍退订（finally 保证）。"""
    _install_shared_stubs(monkeypatch)
    adapter = _shared_adapter()

    async def scenario() -> None:
        runtime = await adapter._ensure_runtime(("u", "p"))  # 预热：跳过启动检查点
        task = asyncio.ensure_future(adapter.fetch_quote(_quote()))
        await asyncio.sleep(0.02)  # 已进入 _await_snapshot 轮询
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.unsubscribed == ["SHFE.au2612"]

    asyncio.run(scenario())

    runtime = _StubRuntime.instances[-1]
    assert runtime.subscribed == ["SHFE.au2612"]
    assert runtime.unsubscribed == ["SHFE.au2612"]


def test_quote_lease_concurrent_same_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发同 symbol 快照请求：引用计数成对增减，互不干扰。"""
    _install_shared_stubs(monkeypatch)
    adapter = _shared_adapter()
    runtime = asyncio.run(adapter._ensure_runtime(("u", "p")))
    # 快照桩需带合法 datetime（响应 mapper 要求，空值显式抛错）
    runtime.snapshots["SHFE.au2612"] = {
        "datetime": "2026-11-13 10:00:00.000000",
        "last_price": 100.0,
    }

    async def scenario() -> None:
        await asyncio.gather(
            adapter.fetch_quote(_quote()), adapter.fetch_quote(_quote())
        )

    asyncio.run(scenario())

    assert len(runtime.subscribed) == 2 and len(runtime.unsubscribed) == 2
