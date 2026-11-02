"""Twelve Data 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from tapehub.domain.errors import ProviderNotImplementedError, UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, DataVariant, Instrument
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tapehub.settings import Settings
from tests.fixtures.providers.stub_twelvedata import (
    forex_values_rows,
    install,
    preset_values,
    sized_values_rows,
)

UTC = timezone.utc


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.FOREX,
        "instrument": Instrument.parse("GLOBAL-OTC-EUR/USD"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=UTC),
        "end": datetime(2026, 1, 10, tzinfo=UTC),
        "adjust": Adjust.NONE,
        "variant": DataVariant.EASTMONEY,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _adapter(settings: Settings | None = None) -> Any:
    from tapehub.providers.twelvedata.adapter import ProviderAdapter

    manifest = load_builtin_manifest("twelvedata")
    record = build_provider_record(
        "twelvedata",
        manifest,
        ProviderAdapter,
        settings=settings or Settings(twelvedata_api_key="k"),
    )
    return record.adapter


def test_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_values(
        monkeypatch, forex_values_rows(["2026-01-05 00:00:00", "2026-01-06 00:00:00"])
    )
    adapter = _adapter(Settings(twelvedata_api_key="test-key"))

    batch = asyncio.run(adapter.fetch_bars(_query()))

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    client = StubTDClient.instances[-1]
    assert client.apikey == "test-key"  # settings 注入链路
    kwargs = client.calls[0]
    # 所有参数显式传递（上游 outputsize 默认 30，必须显式传上限）
    assert kwargs == {
        "symbol": "EUR/USD",
        "interval": "1day",
        "outputsize": 5000,
        "start_date": "2026-01-01T00:00:00",
        "end_date": "2026-01-10T00:00:00",
    }

    assert batch.provider == "twelvedata"
    assert batch.count == 2
    bar = batch.bars[0]
    assert bar.timestamp == datetime(2026, 1, 5, tzinfo=UTC)  # 外汇按 UTC
    assert bar.close == 1.0855
    assert bar.volume is None  # 外汇无 volume 字段
    assert bar.amount is None  # 上游无成交额字段


def test_values_order_desc_normalized_ascending(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游 values 默认倒序（最新在前），适配器必须统一升序输出。"""
    install(monkeypatch)
    preset_values(monkeypatch, forex_values_rows(["2026-01-06", "2026-01-05"]))
    adapter = _adapter()

    batch = asyncio.run(adapter.fetch_bars(_query()))
    timestamps = [bar.timestamp for bar in batch.bars]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == datetime(2026, 1, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("PT1M", "1min"),
        ("PT5M", "5min"),
        ("PT15M", "15min"),
        ("PT30M", "30min"),
        ("PT1H", "1h"),
        ("P1D", "1day"),
        ("P1W", "1week"),
        ("P1M", "1month"),  # 月线，区别于分钟 PT1M
    ],
)
def test_interval_codes(
    interval: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse(interval))))

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    assert StubTDClient.instances[-1].calls[0]["interval"] == expected


def test_crypto_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_values(monkeypatch, forex_values_rows(["2026-01-05 00:00:00"]))
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CRYPTO,
        instrument=Instrument.parse("GLOBAL-CRYPTO-BTC/USD"),
    )
    asyncio.run(adapter.fetch_bars(query))

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    assert StubTDClient.instances[-1].calls[0]["symbol"] == "BTC/USD"


def test_wrong_exchange_namespace_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """外汇必须使用 OTC 命名空间：exchange 段不符时映射层明确拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(instrument=Instrument.parse("GLOBAL-NYSE-EUR/USD"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_unsupported_asset_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twelve Data 仅声明外汇/加密货币：股票请求在映射层拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(asset_type=AssetType.STOCK, instrument=Instrument.parse("CN-SSE-600000"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_unsupported_interval_rejected_by_operation_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse("P3D")))


def test_missing_api_key() -> None:
    adapter = _adapter(Settings(twelvedata_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "API key" in str(excinfo.value)


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "twelvedata", None)  # 模拟 SDK 未安装
    adapter = _adapter(Settings(twelvedata_api_key="k"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "not installed" in str(excinfo.value)


def test_closed_interval_filter_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_values(
        monkeypatch,
        forex_values_rows(
            ["2025-12-31", "2026-01-02", "2026-01-03", "2026-02-01"]
        ),
    )
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query(offset=1, limit=1)))

    assert batch.count == 1
    assert batch.offset == 1
    assert batch.bars[0].timestamp == datetime(2026, 1, 3, tzinfo=UTC)


def test_partial_when_window_not_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回数量达上限且最早一根晚于请求 start：更早数据取不到，标记 partial。"""
    install(monkeypatch)
    # 上游只返回 2026-01-06 起的 5000 根，最早一根晚于请求 start（2026-01-05）
    preset_values(monkeypatch, sized_values_rows(5000, start_day="2026-01-06"))
    adapter = _adapter()
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 20, tzinfo=UTC),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    assert batch.partial is True


def test_partial_false_when_bars_cover_start(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_values(monkeypatch, sized_values_rows(5000, start_day="2025-12-31"))
    adapter = _adapter()
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 20, tzinfo=UTC),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    assert batch.partial is False
