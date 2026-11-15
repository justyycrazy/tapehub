"""AlphaFeed 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime
from typing import Any

import pytest

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    UnsupportedCapabilityError,
    UpstreamError,
    UpstreamRateLimitedError,
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
from tests.fixtures.providers.stub_alphafeed import (
    StubRateLimitError,
    install,
    klines_payload,
    preset_error,
    preset_payload,
    preset_quote_payload,
    quote_payload,
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


@pytest.mark.parametrize(
    "interval",
    ["PT1M", "PT5M", "PT15M", "PT30M", "PT1H"],
)
def test_minute_periods_not_supported(
    interval: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分钟族不声明（套餐无分钟 K 线权限）：二级路由必须拒绝，落到回退链。"""
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse(interval)))


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
        ("HK-HKEX-00700", "00700.HK"),
        ("US-NASDAQ-AAPL", "AAPL.US"),
        ("US-NYSE-IBM", "IBM.US"),
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


def test_ambiguous_batch_payload_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批量包装多标的响应：无法判定唯一标的，显式拒绝而非静默取第一个。"""
    install(monkeypatch)
    columns = klines_payload(["2026-01-02"])["data"]
    preset_payload(monkeypatch, {"600519.SH": columns, "000001.SZ": dict(columns)})
    adapter = _adapter()

    with pytest.raises(ValueError, match="multiple symbol entries"):
        asyncio.run(adapter.fetch_bars(_query()))


def test_unsupported_exchange_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider 范围外的 CN 交易所（如中证 CSI）映射层拒绝；market/exchange
    矛盾组合（如 JP-TSE-…、CN-NYSE-…）由域层词汇表在构造期拦截
    （见 test_models），不再到达映射层。CN 三交易所已支持，不得误判。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(instrument=Instrument.parse("CN-CSI-000905"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def _quote_query(**overrides: Any) -> QuoteQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600519"),
        "variant": DataVariant.ALPHAFEED,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def test_quote_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """快照映射：单标的 symbols 调用、字段归一化与 extensions 白名单。"""
    install(monkeypatch)
    preset_quote_payload(monkeypatch, quote_payload())
    adapter = _adapter()

    quote = asyncio.run(adapter.fetch_quote(_quote_query()))

    from tests.fixtures.providers.stub_alphafeed import StubAlphaFeed

    # af.quotes.get keyword-only 单元素 symbols（5 标的/次批量容量闲置）
    assert StubAlphaFeed.instances[-1].calls[0] == {"symbols": ["600519.SH"]}
    assert quote.provider == "alphafeed"
    assert quote.last_price == 20.25
    assert quote.volume == 200.0
    assert quote.amount == 4040.0
    # 桩按 UTC 零点编码，归一化输出即北京 08:00
    assert quote.timestamp == datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE)
    assert quote.received_at.tzinfo is not None
    assert quote.extensions is not None
    assert quote.extensions["region"] == "CN"
    assert quote.extensions["prev_close"] == 20.1
    assert quote.extensions["name"] == "测试标的"
    assert quote.extensions["change_pct"] == 0.00746  # 小数比例（0.746%）
    # ext.type 不在白名单，不受控字段不透出
    assert "type" not in quote.extensions
    # 五档盘口上游无源字段，恒 null 不冒充
    assert quote.bid_price1 is None
    assert quote.ask_price5 is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("HK-HKEX-00700", ["00700.HK"]),
        ("US-NASDAQ-AAPL", ["AAPL.US"]),
    ],
)
def test_quote_hk_us_symbols(
    code: str, expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_quote_payload(monkeypatch, quote_payload(symbol=expected[0]))
    adapter = _adapter()
    asyncio.run(adapter.fetch_quote(_quote_query(instrument=Instrument.parse(code))))

    from tests.fixtures.providers.stub_alphafeed import StubAlphaFeed

    assert StubAlphaFeed.instances[-1].calls[0]["symbols"] == expected


def test_quote_missing_last_price_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """last_price 缺失属上游契约违背：抛 UPSTREAM_ERROR，不用 0 冒充。"""
    install(monkeypatch)
    payload = quote_payload()
    payload[0]["last_price"] = None
    preset_quote_payload(monkeypatch, payload)
    adapter = _adapter()

    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(_quote_query()))


def test_quote_missing_timestamp_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """timestamp 缺失属上游契约违背：抛 UPSTREAM_ERROR，不伪造时间。"""
    install(monkeypatch)
    payload = quote_payload()
    payload[0]["timestamp"] = None
    preset_quote_payload(monkeypatch, payload)
    adapter = _adapter()

    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(_quote_query()))


def test_rate_limit_maps_to_upstream_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 429（重试耗尽后）→ UPSTREAM_RATE_LIMITED，retry_after_ms 透传 details。"""
    install(monkeypatch)
    preset_error(monkeypatch, StubRateLimitError(details={"retry_after_ms": 250}))
    adapter = _adapter()

    with pytest.raises(UpstreamRateLimitedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details == {"provider": "alphafeed", "retry_after_ms": 250}


def test_rate_limit_without_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游 429 未携带 retry_after_ms：如实省略，不编造。"""
    install(monkeypatch)
    preset_error(monkeypatch, StubRateLimitError())
    adapter = _adapter()

    with pytest.raises(UpstreamRateLimitedError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert excinfo.value.details == {"provider": "alphafeed"}
