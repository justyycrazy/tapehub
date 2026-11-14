"""Twelve Data 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime
from typing import Any

import pytest

from tapehub.domain.errors import (
    ProviderNotImplementedError,
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
from tests.fixtures.providers.stub_twelvedata import (
    forex_values_rows,
    install,
    preset_error,
    preset_values,
    sized_values_rows,
    values_rows,
)


class BadRequestError(Exception):
    """模拟 twelvedata SDK 的 400 异常（适配器按类型名 + 实测指纹识别）。"""


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.FOREX,
        "instrument": Instrument.parse("GLOBAL-OTC-EUR/USD"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.NONE,
        "variant": DataVariant.TWELVEDATA,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _quote_query(**overrides: Any) -> QuoteQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.FOREX,
        "instrument": Instrument.parse("GLOBAL-OTC-EUR/USD"),
        "variant": DataVariant.TWELVEDATA,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def _adapter(settings: Settings | None = None) -> Any:
    from tapehub.providers.twelvedata.adapter import ProviderAdapter

    manifest = load_builtin_manifest("twelvedata")
    record = build_provider_record(
        "twelvedata",
        manifest,
        ProviderAdapter,
        settings=settings or make_settings(twelvedata_api_key="k"),
    )
    return record.adapter


def test_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_values(
        monkeypatch, forex_values_rows(["2026-01-05 00:00:00", "2026-01-06 00:00:00"])
    )
    adapter = _adapter(make_settings(twelvedata_api_key="test-key"))

    batch = asyncio.run(adapter.fetch_bars(_query()))

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    client = StubTDClient.instances[-1]
    assert client.apikey == "test-key"  # settings 注入链路
    kwargs = client.calls[0]
    # 所有参数显式传递（上游 outputsize 默认 30，必须显式传上限）；
    # 边界从北京时间转悉尼墙钟（1 月悉尼 AEDT=UTC+11，比北京快 3 小时）
    assert kwargs == {
        "symbol": "EUR/USD",
        "interval": "1day",
        "outputsize": 5000,
        "start_date": "2026-01-01T03:00:00",
        "end_date": "2026-01-10T03:00:00",
    }

    assert batch.provider == "twelvedata"
    assert batch.count == 2
    bar = batch.bars[0]
    # 外汇墙钟按悉尼解释：悉尼 2026-01-05 00:00 = 北京 2026-01-04 21:00
    assert bar.timestamp == datetime(2026, 1, 4, 21, tzinfo=CN_TIMEZONE)
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
    assert timestamps[0] == datetime(2026, 1, 4, 21, tzinfo=CN_TIMEZONE)


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("PT1M", "1min"),
        ("PT5M", "5min"),
        ("PT15M", "15min"),
        ("PT30M", "30min"),
        ("PT45M", "45min"),
        ("PT1H", "1h"),
        ("PT2H", "2h"),
        ("PT4H", "4h"),
        ("PT8H", "8h"),
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
    batch = asyncio.run(adapter.fetch_bars(query))

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    assert StubTDClient.instances[-1].calls[0]["symbol"] == "BTC/USD"
    # 加密货币墙钟按 UTC 解释（区别于外汇的 Sydney）：UTC 00:00 = 北京 08:00
    assert batch.bars[0].timestamp == datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE)


# ---------------------------------------------------------------------------
# 美股 stock/etf（US market）：exchange/adj 显式传参、纽约墙钟、白名单
# ---------------------------------------------------------------------------


def _us_query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("US-NASDAQ-AAPL"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.QFQ,
        "variant": DataVariant.TWELVEDATA,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def test_us_stock_request_mapping_qfq(monkeypatch: pytest.MonkeyPatch) -> None:
    """美股显式传 exchange（消歧）与 adjust：qfq → "all"（拆股+分红前复权）。"""
    install(monkeypatch)
    preset_values(monkeypatch, values_rows(["2026-01-05"]))
    adapter = _adapter()

    asyncio.run(adapter.fetch_bars(_us_query()))

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    kwargs = StubTDClient.instances[-1].calls[0]
    # 边界从北京时间转纽约墙钟（1 月纽约 EST=UTC-5，比北京慢 13 小时）
    assert kwargs == {
        "symbol": "AAPL",
        "interval": "1day",
        "outputsize": 5000,
        "start_date": "2025-12-31T11:00:00",
        "end_date": "2026-01-09T11:00:00",
        "exchange": "NASDAQ",
        "adjust": "all",
    }


def test_us_stock_request_boundary_edt(monkeypatch: pytest.MonkeyPatch) -> None:
    """夏令时（EDT=UTC-4）边界换算：北京 7 月 1 日零点 → 纽约 6 月 30 日 12:00。"""
    install(monkeypatch)
    preset_values(monkeypatch, values_rows(["2026-07-01"]))
    adapter = _adapter()

    asyncio.run(
        adapter.fetch_bars(
            _us_query(
                start=datetime(2026, 7, 1, tzinfo=CN_TIMEZONE),
                end=datetime(2026, 7, 10, tzinfo=CN_TIMEZONE),
            )
        )
    )

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    kwargs = StubTDClient.instances[-1].calls[0]
    assert kwargs["start_date"] == "2026-06-30T12:00:00"
    assert kwargs["end_date"] == "2026-07-09T12:00:00"


def test_us_stock_request_adjust_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """adjust=none 显式映射 adjust="none"（原始价）。"""
    install(monkeypatch)
    preset_values(monkeypatch, values_rows(["2026-01-05"]))
    adapter = _adapter()

    asyncio.run(adapter.fetch_bars(_us_query(adjust=Adjust.NONE)))

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    assert StubTDClient.instances[-1].calls[0]["adjust"] == "none"


def test_us_stock_hfq_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游无后复权等价物：hfq 在 operation 二级路由拒绝（能力目录即未声明）。"""
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(_us_query(adjust=Adjust.HFQ)))


def test_us_stock_response_mapping_date_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """美股日线 date-only 按纽约零点解释：EST 期落北京 13:00、EDT 期落 12:00。"""
    install(monkeypatch)
    preset_values(monkeypatch, values_rows(["2026-01-05", "2026-07-01"]))
    adapter = _adapter()

    batch = asyncio.run(
        adapter.fetch_bars(
            _us_query(end=datetime(2026, 7, 31, tzinfo=CN_TIMEZONE))
        )
    )

    assert [bar.timestamp for bar in batch.bars] == [
        datetime(2026, 1, 5, 13, tzinfo=CN_TIMEZONE),  # EST（UTC-5）
        datetime(2026, 7, 1, 12, tzinfo=CN_TIMEZONE),  # EDT（UTC-4）
    ]
    bar = batch.bars[0]
    assert bar.volume == 12345.0  # 美股 volume 有值（区别于外汇/加密货币）
    assert bar.amount is None  # 上游无成交额字段


def test_us_etf_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """美股 ETF：exchange 白名单含 NYSE，复权仅声明 none。"""
    install(monkeypatch)
    preset_values(monkeypatch, values_rows(["2026-01-05"]))
    adapter = _adapter()

    asyncio.run(
        adapter.fetch_bars(
            _us_query(
                asset_type=AssetType.ETF,
                instrument=Instrument.parse("US-NYSE-SPY"),
                adjust=Adjust.NONE,
            )
        )
    )

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    kwargs = StubTDClient.instances[-1].calls[0]
    assert kwargs["symbol"] == "SPY"
    assert kwargs["exchange"] == "NYSE"
    assert kwargs["adjust"] == "none"


def test_us_etf_qfq_maps_to_adjust_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """ETF qfq 已声明（探针实证复权生效）：映射上游 adjust=all。"""
    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    install(monkeypatch)
    preset_values(monkeypatch, values_rows(["2026-01-05"]))
    adapter = _adapter()

    asyncio.run(
        adapter.fetch_bars(
            _us_query(
                asset_type=AssetType.ETF,
                instrument=Instrument.parse("US-NYSE-SPY"),
                adjust=Adjust.QFQ,
            )
        )
    )
    kwargs = StubTDClient.instances[-1].calls[0]
    assert kwargs["adjust"] == "all"  # etf 与 stock 同映射：qfq → adjust=all


def test_us_cboe_exchange_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """CBOE 在美股白名单内（目录 1,502 个 ETF；准入经探针核实）。"""
    install(monkeypatch)
    preset_values(monkeypatch, values_rows(["2026-01-05"]))
    adapter = _adapter()

    asyncio.run(
        adapter.fetch_bars(
            _us_query(
                asset_type=AssetType.ETF,
                instrument=Instrument.parse("US-CBOE-AAAA"),
                adjust=Adjust.NONE,
            )
        )
    )

    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    assert StubTDClient.instances[-1].calls[0]["exchange"] == "CBOE"


@pytest.mark.parametrize("instrument", ["US-OTC-AABB", "US-IEX-AAC"])
def test_us_exchange_whitelist_rejected(
    instrument: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OTC（粉单）与 IEX 不在美股白名单：exchange 段校验明确拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _us_query(asset_type=AssetType.STOCK, instrument=Instrument.parse(instrument))
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        asyncio.run(adapter.fetch_bars(query))
    # 错误信息列出全部允许的 exchange 段
    assert "NYSE" in str(excinfo.value)


def test_no_data_window_normalized_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测核实的空窗指纹（区间无数据 → 400）归一化为空结果，不作为错误上抛。"""
    install(monkeypatch)
    preset_error(
        monkeypatch,
        BadRequestError(
            '{"code":400,"message":"No data is available on the specified dates. '
            'Try setting different start/end dates.","status":"error"}'
        ),
    )
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query()))

    assert batch.bars == []
    assert batch.partial is False


def test_other_bad_request_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """指纹外的其余 400 不属于空窗结论：照常上抛，由应用层包装为 UPSTREAM_ERROR。"""
    install(monkeypatch)
    preset_error(
        monkeypatch,
        BadRequestError('{"code":400,"message":"invalid symbol","status":"error"}'),
    )
    adapter = _adapter()
    with pytest.raises(BadRequestError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_wrong_exchange_namespace_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """外汇必须使用 OTC 命名空间：exchange 段不符时映射层明确拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(instrument=Instrument.parse("GLOBAL-NYSE-EUR/USD"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_unsupported_asset_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twelve Data 仅声明外汇/加密货币/美股股票与 ETF：期货请求在映射层拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CONTRACT, instrument=Instrument.parse("CN-SHFE-RB2412")
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_cn_market_stock_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """股票能力仅声明 US 市场：CN-SSE 标的在 operation 二级路由拒绝。"""
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
    adapter = _adapter(make_settings(twelvedata_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "API key" in str(excinfo.value)


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "twelvedata", None)  # 模拟 SDK 未安装
    adapter = _adapter(make_settings(twelvedata_api_key="k"))
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
    # 窗内悉尼日 2026-01-02/03 → 北京 2026-01-01/02 21:00，offset=1 取第二根
    assert batch.bars[0].timestamp == datetime(2026, 1, 2, 21, tzinfo=CN_TIMEZONE)


def test_partial_when_window_not_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回数量达上限且最早一根晚于请求 start：更早数据取不到，标记 partial。"""
    install(monkeypatch)
    # 上游只返回 2026-01-06 起的 5000 根，最早一根晚于请求 start（2026-01-05）
    preset_values(monkeypatch, sized_values_rows(5000, start_day="2026-01-06"))
    adapter = _adapter()
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=CN_TIMEZONE),
        end=datetime(2026, 1, 20, tzinfo=CN_TIMEZONE),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    assert batch.partial is True


def test_partial_false_when_bars_cover_start(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_values(monkeypatch, sized_values_rows(5000, start_day="2025-12-31"))
    adapter = _adapter()
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 5, tzinfo=CN_TIMEZONE),
        end=datetime(2026, 1, 20, tzinfo=CN_TIMEZONE),
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    assert batch.partial is False


# ---------------------------------------------------------------------------
# 实时快照：急切校验（快照/快照流已改走 WS 订阅源，链路用例见
# test_twelvedata_ws_runtime.py 与 test_twelvedata_stream.py）
# ---------------------------------------------------------------------------


def test_quote_unsupported_asset_type() -> None:
    """quotes 能力仅声明 forex/crypto：股票快照请求在二级路由拒绝。"""
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_quote_operation(
            _quote_query(
                asset_type=AssetType.STOCK,
                instrument=Instrument.parse("CN-SSE-600000"),
            )
        )


def test_quote_missing_api_key() -> None:
    adapter = _adapter(make_settings(twelvedata_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert "API key" in str(excinfo.value)
