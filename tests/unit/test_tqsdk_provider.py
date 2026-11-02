"""TqSdk 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

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
from tests.fixtures.providers.stub_tqsdk import (
    install,
    klines_rows,
    preset_rows,
    sized_klines_rows,
)

UTC = timezone.utc


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.CONTRACT,
        "instrument": Instrument.parse("CN-SHFE-cu2607"),
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
    from tapehub.providers.tqsdk.adapter import ProviderAdapter

    manifest = load_builtin_manifest("tqsdk")
    record = build_provider_record(
        "tqsdk",
        manifest,
        ProviderAdapter,
        settings=settings
        or Settings(tqsdk_auth_user="user@qq.com", tqsdk_auth_password="secret"),
    )
    return record.adapter


def test_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    StubTqApi = install(monkeypatch)
    preset_rows(monkeypatch, klines_rows(["2026-01-05", "2026-01-06"]))
    adapter = _adapter(Settings(tqsdk_auth_user="user@qq.com", tqsdk_auth_password="secret"))

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
        "data_length": 10000,
    }

    assert batch.provider == "tqsdk"
    assert batch.count == 2
    bar = batch.bars[0]
    assert bar.timestamp == datetime(2026, 1, 5, tzinfo=UTC)
    assert bar.close == 3005.0
    assert bar.volume == 500.0  # 期货成交量保持手
    assert bar.amount is None  # 上游无成交额字段
    assert bar.extensions is not None
    assert bar.extensions["open_oi"] == 100000.0
    assert bar.extensions["close_oi"] == 101000.0


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
        (AssetType.CONTRACT, "CN-SHFE-cu2607", "SHFE.cu2607"),
        (AssetType.CONTRACT, "CN-CZCE-SR609", "CZCE.SR609"),
        (AssetType.CONTINUOUS, "CN-SHFE-cu", "KQ.m@SHFE.cu"),
        (AssetType.WEIGHTED_INDEX, "CN-SHFE-bu", "KQ.i@SHFE.bu"),
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
    adapter = _adapter(Settings(tqsdk_auth_user="", tqsdk_auth_password="pwd"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["missing"] == ["user"]


def test_missing_password() -> None:
    adapter = _adapter(Settings(tqsdk_auth_user="user@qq.com", tqsdk_auth_password=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["missing"] == ["password"]


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tqsdk", None)  # import 失败，模拟 SDK 未安装
    adapter = _adapter(
        Settings(tqsdk_auth_user="user@qq.com", tqsdk_auth_password="secret")
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
    assert batch.bars[0].timestamp == datetime(2026, 1, 3, tzinfo=UTC)


def test_partial_when_window_not_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回数量达上限且最早一根晚于请求 start：更早数据取不到，标记 partial。"""
    install(monkeypatch)
    # 10000 根分钟线，从 2026-01-01T00:00Z 开始（覆盖 start）→ 不算 partial
    adapter = _adapter()
    covered = asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse("PT1M"))))
    assert covered.partial is False

    # 上游只返回 2026-01-06 起的 10000 根，最早一根晚于请求 start（2026-01-05）
    # → start 之前的更早数据取不到，标记 partial
    from tests.fixtures.providers.stub_tqsdk import StubTqApi

    StubTqApi.instances.clear()
    preset_rows(monkeypatch, sized_klines_rows(10000, start_day="2026-01-06"))
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 20, tzinfo=UTC),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    assert batch.partial is True


def test_partial_false_when_bars_cover_start(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_rows(monkeypatch, sized_klines_rows(10000, start_day="2025-12-31"))
    adapter = _adapter()
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=UTC),
        end=datetime(2026, 1, 20, tzinfo=UTC),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    # 最早一根早于 start：窗口完整覆盖，即使达到数量上限也不标记 partial
    assert batch.partial is False
