"""TickFlow 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

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
from tests.fixtures.providers.stub_tickflow import (
    install,
    klines_payload,
    preset_payload,
    preset_quote_payload,
    quote_payload,
    sized_klines_payload,
)


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.QFQ,
        "variant": DataVariant.TICKFLOW,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _adapter(settings: Settings | None = None) -> Any:
    from tapehub.providers.tickflow.adapter import ProviderAdapter

    manifest = load_builtin_manifest("tickflow")
    record = build_provider_record(
        "tickflow",
        manifest,
        ProviderAdapter,
        settings=settings or make_settings(tickflow_api_key="k"),
    )
    return record.adapter


def test_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = install(monkeypatch)
    adapter = _adapter(make_settings(tickflow_api_key="test-key"))
    batch = asyncio.run(adapter.fetch_bars(_query()))

    created = clients[-1]
    assert created.api_key == "test-key"  # settings 注入链路
    kwargs = created.calls[0]
    # 所有参数显式传递
    assert kwargs["symbol"] == "600000.SH"
    assert kwargs["period"] == "1d"
    assert kwargs["adjust"] == "forward"  # qfq → 比例前复权
    assert kwargs["count"] == 10000
    assert kwargs["start_time"] == int(datetime(2026, 1, 1, tzinfo=CN_TIMEZONE).timestamp() * 1000)
    assert kwargs["end_time"] == int(datetime(2026, 1, 10, tzinfo=CN_TIMEZONE).timestamp() * 1000)

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
    # 桩按 UTC 零点编码日界，归一化输出即北京 08:00
    assert bar.timestamp == datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE)
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
    adapter = _adapter(make_settings(tickflow_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "API key" in str(excinfo.value)


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tickflow", None)  # import 失败，模拟 SDK 未安装
    adapter = _adapter(make_settings(tickflow_api_key="k"))
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


def test_unsupported_interval_rejected_by_operation_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse("P3D")))


# ── 周期上界：项目最大到月线，P1Y 不声明 ──────────────────────────────


def test_period_p1y_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1Y 是合法 ISO 周期但超出项目声明范围：映射层即拒绝。"""
    from tapehub.providers.tickflow.request_mapper import period_code

    with pytest.raises(UnsupportedCapabilityError):
        period_code(Interval.parse("P1Y"))


# ── 港股 / 美股 symbol 后缀 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("HK-HKEX-00700", "00700.HK"),
        ("US-NASDAQ-AAPL", "AAPL.US"),
        ("US-NYSE-IBM", "IBM.US"),
        ("US-CBOE-SPX", "SPX.US"),
    ],
)
def test_hk_us_symbol_suffixes(
    code: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(instrument=Instrument.parse(code))))
    from tests.fixtures.providers.stub_tickflow import StubTickFlow

    assert StubTickFlow.instances[-1].calls[0]["symbol"] == expected


def test_unsupported_exchange_rejected() -> None:
    from tapehub.providers.tickflow.request_mapper import symbol_code

    with pytest.raises(UnsupportedCapabilityError):
        symbol_code(Instrument.parse("GLOBAL-OTC-EUR/USD"))


# ── 实时快照（quotes） ──────────────────────────────────────────────


def _quote_query(**overrides: Any) -> Any:
    from tapehub.domain.models import QuoteQuery

    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "variant": DataVariant.TICKFLOW,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def test_fetch_quote_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_quote_payload(monkeypatch, quote_payload("600000.SH"))
    adapter = _adapter(make_settings(tickflow_api_key="test-key"))

    quote = asyncio.run(adapter.fetch_quote(_quote_query()))

    from tests.fixtures.providers.stub_tickflow import StubTickFlow

    client = StubTickFlow.instances[-1]
    # 单标的快照：symbols 单元素列表，无其它参数
    assert client.quote_calls[0] == {"symbols": ["600000.SH"]}
    assert quote.provider == "tickflow"
    assert quote.last_price == 10.25
    assert quote.volume == 123456.0
    assert quote.amount == 1266924.0
    # 桩按 UTC 零点编码时间戳，归一化输出即北京 08:00
    assert quote.timestamp == datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE)
    # 盘口上游无源字段，恒 null
    assert quote.bid_price1 is None and quote.ask_price1 is None
    # extensions 白名单：会话/名称/涨跌幅与开高低昨收
    ext = quote.extensions or {}
    assert ext["session"] == "regular"
    assert ext["region"] == "CN"
    assert ext["name"] == "浦发银行"
    assert ext["change_pct"] == 1.49
    assert ext["change_amount"] == 0.15
    assert ext["amplitude"] == 3.92
    assert ext["turnover_rate"] == 0.04
    assert ext["prev_close"] == 10.1
    assert ext["open"] == 10.0 and ext["high"] == 10.3 and ext["low"] == 9.9


def test_fetch_quote_hk_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_quote_payload(monkeypatch, quote_payload("00700.HK"))
    adapter = _adapter()

    quote = asyncio.run(
        adapter.fetch_quote(_quote_query(instrument=Instrument.parse("HK-HKEX-00700")))
    )

    from tests.fixtures.providers.stub_tickflow import StubTickFlow

    assert StubTickFlow.instances[-1].quote_calls[0] == {"symbols": ["00700.HK"]}
    assert quote.last_price == 10.25


def test_fetch_quote_contract_violation_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """timestamp 缺失 / 空 data：上游契约违背 → UPSTREAM_ERROR（不伪造）。"""
    from tapehub.domain.errors import TapehubError, UpstreamError

    install(monkeypatch)
    preset_quote_payload(monkeypatch, {"data": []})
    adapter = _adapter()
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(_quote_query()))

    preset_quote_payload(monkeypatch, {"data": [{"last_price": 1.0}]})
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert issubclass(UpstreamError, TapehubError)


def test_fetch_quote_missing_api_key() -> None:
    from tapehub.domain.errors import ProviderNotImplementedError

    adapter = _adapter(make_settings())
    with pytest.raises(ProviderNotImplementedError):
        asyncio.run(adapter.fetch_quote(_quote_query()))
