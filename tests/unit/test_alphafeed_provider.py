"""AlphaFeed 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime
from typing import Any

import pytest

from tapehub.domain.errors import ProviderNotImplementedError, UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import CN_TIMEZONE, Adjust, AssetType, BarQuery, DataVariant, Instrument
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tapehub.settings import Settings
from tests.conftest import make_settings
from tests.fixtures.providers.stub_alphafeed import (
    install,
    klines_payload,
    preset_payload,
    sized_klines_payload,
)


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600519"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.QFQ,
        "variant": DataVariant.ALPHAFEED,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _adapter(settings: Settings | None = None) -> Any:
    from tapehub.providers.alphafeed.adapter import ProviderAdapter

    manifest = load_builtin_manifest("alphafeed")
    record = build_provider_record(
        "alphafeed",
        manifest,
        ProviderAdapter,
        settings=settings or make_settings(alphafeed_api_key="k"),
    )
    return record.adapter


def test_request_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = install(monkeypatch)
    adapter = _adapter(make_settings(alphafeed_api_key="test-key"))
    batch = asyncio.run(adapter.fetch_bars(_query()))

    created = clients[-1]
    assert created.api_key == "test-key"  # settings 注入链路
    kwargs = created.calls[0]
    # 所有参数显式传递
    assert kwargs["symbol"] == "600519.SH"
    assert kwargs["period"] == "1d"
    assert kwargs["adjust"] == "forward"  # qfq → 前复权
    assert kwargs["count"] == 10000
    assert kwargs["start_time"] == int(datetime(2026, 1, 1, tzinfo=CN_TIMEZONE).timestamp() * 1000)
    assert kwargs["end_time"] == int(datetime(2026, 1, 10, tzinfo=CN_TIMEZONE).timestamp() * 1000)

    assert batch.provider == "alphafeed"
    assert batch.partial is False


def test_response_mapping_with_data_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, klines_payload(["2026-01-05", "2026-01-06"]))
    adapter = _adapter()

    batch = asyncio.run(adapter.fetch_bars(_query()))

    assert batch.count == 2
    bar = batch.bars[0]
    # 桩按 UTC 零点编码日界，归一化输出即北京 08:00
    assert bar.timestamp == datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE)
    assert bar.open == 20.0
    assert bar.close == 20.2
    assert bar.volume == 200.0
    assert bar.amount == 4040.0
    assert bar.extensions is not None
    assert bar.extensions["prev_close"] == 20.1


def test_pre_1970_timestamp_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """1970 前负毫秒时间戳映射（纪元算术，Windows fromtimestamp 会崩）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, klines_payload(["1960-06-15", "1960-06-16"]))
    adapter = _adapter()

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
        ("PT1M", "1m"),
        ("PT5M", "5m"),
        ("PT15M", "15m"),
        ("PT30M", "30m"),
        ("PT1H", "60m"),
        ("P1W", "1w"),
        ("P1M", "1M"),  # 月线，区别于分钟 PT1M
    ],
)
def test_period_codes(
    interval: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse(interval))))

    from tests.fixtures.providers.stub_alphafeed import StubAlphaFeed

    assert StubAlphaFeed.instances[-1].calls[0]["period"] == expected


def test_pt10m_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """AlphaFeed 周期枚举没有 10m：二级路由必须拒绝，不得复用相邻周期。"""
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse("PT10M")))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("CN-SSE-600519", "600519.SH"),
        ("CN-SZSE-000001", "000001.SZ"),
        ("CN-BSE-920047", "920047.BJ"),
    ],
)
def test_symbol_suffixes(
    code: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(instrument=Instrument.parse(code))))

    from tests.fixtures.providers.stub_alphafeed import StubAlphaFeed

    assert StubAlphaFeed.instances[-1].calls[0]["symbol"] == expected


def test_adjust_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(adjust=Adjust.NONE)))

    from tests.fixtures.providers.stub_alphafeed import StubAlphaFeed

    assert StubAlphaFeed.instances[-1].calls[0]["adjust"] == "none"


def test_missing_api_key() -> None:
    adapter = _adapter(make_settings(alphafeed_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "API key" in str(excinfo.value)


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "alphafeed", None)  # import 失败，模拟 SDK 未安装
    adapter = _adapter(make_settings(alphafeed_api_key="k"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "not installed" in str(excinfo.value)


def test_closed_interval_filter_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        # 上游返回窗口外数据 + 窗口内数据，适配器按 [start, end] 精确复核
        klines_payload(["2025-12-31", "2026-01-02", "2026-01-03", "2026-02-01"]),
    )
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query(offset=1, limit=1)))

    assert batch.count == 1
    assert batch.offset == 1
    assert batch.bars[0].timestamp == datetime(2026, 1, 3, 8, tzinfo=CN_TIMEZONE)


def test_partial_when_upstream_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, sized_klines_payload(10000))
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query()))
    assert batch.partial is True  # 达到上游上限，可能截断


def test_unsupported_exchange_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    adapter = _adapter()
    query = _query(instrument=Instrument.parse("CN-HKEX-00700"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))
