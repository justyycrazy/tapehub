"""TickFlow 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

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
from tests.fixtures.providers.stub_tickflow import (
    install,
    klines_payload,
    preset_payload,
    sized_klines_payload,
)

UTC = timezone.utc


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=UTC),
        "end": datetime(2026, 1, 10, tzinfo=UTC),
        "adjust": Adjust.QFQ,
        "variant": DataVariant.EASTMONEY,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _adapter(settings: Settings | None = None) -> Any:
    from tapehub.providers.tickflow.adapter import ProviderAdapter

    manifest = load_builtin_manifest("tickflow")
    record = build_provider_record(
        "tickflow", manifest, ProviderAdapter, settings=settings or Settings(tickflow_api_key="k")
    )
    return record.adapter


def test_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = install(monkeypatch)
    adapter = _adapter(Settings(tickflow_api_key="test-key"))
    batch = asyncio.run(adapter.fetch_bars(_query()))

    created = clients[-1]
    assert created.api_key == "test-key"  # settings 注入链路
    kwargs = created.calls[0]
    # 所有参数显式传递
    assert kwargs["symbol"] == "600000.SH"
    assert kwargs["period"] == "1d"
    assert kwargs["adjust"] == "forward"  # qfq → 比例前复权
    assert kwargs["count"] == 10000
    assert kwargs["start_time"] == int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    assert kwargs["end_time"] == int(datetime(2026, 1, 10, tzinfo=UTC).timestamp() * 1000)

    assert batch.provider == "tickflow"
    assert batch.partial is False
    assert batch.count == 0  # 默认空 payload


def test_full_mapping_with_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, klines_payload(["2026-01-05", "2026-01-06"]))
    adapter = _adapter()

    batch = asyncio.run(adapter.fetch_bars(_query()))

    assert batch.count == 2
    bar = batch.bars[0]
    assert bar.timestamp == datetime(2026, 1, 5, tzinfo=UTC)
    assert bar.open == 10.0
    assert bar.close == 10.2
    assert bar.volume == 100.0
    assert bar.amount == 1020.0
    assert bar.extensions is not None
    assert bar.extensions["prev_close"] == 10.1


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("PT1M", "1m"),
        ("PT5M", "5m"),
        ("PT15M", "15m"),
        ("PT1H", "60m"),
        ("P1W", "1w"),
        ("P1M", "1M"),  # 月线，区别于分钟 PT1M
        ("P1Y", "1Y"),
    ],
)
def test_period_codes(
    interval: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse(interval))))
    assert adapter is not None
    from tests.fixtures.providers.stub_tickflow import StubTickFlow

    assert StubTickFlow.instances[-1].calls[0]["period"] == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("CN-SSE-600000", "600000.SH"),
        ("CN-SZSE-000001", "000001.SZ"),
        ("CN-BSE-430047", "430047.BJ"),
    ],
)
def test_symbol_suffixes(
    code: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(instrument=Instrument.parse(code))))
    from tests.fixtures.providers.stub_tickflow import StubTickFlow

    assert StubTickFlow.instances[-1].calls[0]["symbol"] == expected


def test_adjust_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(adjust=Adjust.NONE)))
    from tests.fixtures.providers.stub_tickflow import StubTickFlow

    assert StubTickFlow.instances[-1].calls[0]["adjust"] == "none"


def test_missing_api_key() -> None:
    adapter = _adapter(Settings(tickflow_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "API key" in str(excinfo.value)


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tickflow", None)  # import 失败，模拟 SDK 未安装
    adapter = _adapter(Settings(tickflow_api_key="k"))
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
    assert batch.bars[0].timestamp == datetime(2026, 1, 3, tzinfo=UTC)


def test_partial_when_upstream_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, sized_klines_payload(10000))
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query()))
    assert batch.partial is True  # 达到上游上限，可能截断


def test_unsupported_interval_rejected_by_operation_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse("P3D")))
