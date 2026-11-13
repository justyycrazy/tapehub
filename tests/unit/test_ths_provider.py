"""ths 适配器单元测试：请求映射、响应映射与运行前提（HTTP 全部 Mock）。"""

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
from tapehub.providers.ths.request_mapper import REST_BASE
from tests.conftest import make_settings
from tests.fixtures.providers.stub_ths import (
    bar_rows,
    envelope,
    install,
    intraday_data,
    intraday_points,
    preset_payload,
    preset_status,
    snapshot_data,
    stock_snapshot_item,
)

# 北京时间边界（域层保证 aware）：2026-01-01 / 2026-01-10 零点 → 毫秒戳
START_MS = 1767196800000  # 2026-01-01T00:00:00+08:00
END_MS = 1767974400000  # 2026-01-10T00:00:00+08:00


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600519"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.QFQ,
        "variant": DataVariant.THS,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _quote_query(**overrides: Any) -> QuoteQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600519"),
        "variant": DataVariant.THS,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def _adapter(settings: Any = None) -> Any:
    from tapehub.providers.ths.adapter import ProviderAdapter

    manifest = load_builtin_manifest("ths")
    record = build_provider_record(
        "ths",
        manifest,
        ProviderAdapter,
        settings=settings or make_settings(ths_api_key="k"),
    )
    return record.adapter


def _last_call() -> dict[str, Any]:
    from tests.fixtures.providers.stub_ths import StubClient

    return StubClient.instances[-1].calls[-1]


# ---------------------------------------------------------------------------
# 历史 K 线：请求映射与响应映射
# ---------------------------------------------------------------------------


def test_stock_bars_request_and_response_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    # date_ms 为 K 线日期（Asia/Shanghai 零点毫秒戳）：首尾两端点 + 窗外一根
    preset_payload(
        monkeypatch,
        envelope({"item": bar_rows("date_ms", [START_MS, START_MS + 86_400_000, END_MS])}),
    )
    adapter = _adapter(make_settings(ths_api_key="test-key"))

    batch = asyncio.run(adapter.fetch_bars(_query()))

    call = _last_call()
    # 端点按 operation 分流；认证经 X-api-key 头（key 不落 URL）；时间边界
    # 显式传毫秒戳、复权显式传（不依赖上游默认值：上游缺省 forward）
    assert call == {
        "url": f"{REST_BASE}/api/a-share/prices/historical",
        "params": {
            "thscode": "600519.SH",
            "interval": "1d",
            "start": START_MS,
            "end": END_MS,
            "adjust": "forward",  # qfq → forward（前复权）
        },
        "headers": {"X-api-key": "test-key"},
    }

    assert batch.provider == "ths"
    assert batch.partial is False  # 上游无文档化行数上限，不编造截断结论
    assert batch.count == 3
    bar = batch.bars[1]
    # date_ms 北京零点 → 统一北京时间；行值按行序递增造数（见 stub bar_rows）
    assert bar.timestamp == datetime(2026, 1, 2, 0, tzinfo=CN_TIMEZONE)
    assert (bar.open, bar.high, bar.low, bar.close) == (11.0, 12.0, 10.5, 11.5)
    assert bar.volume == 1001.0  # A 股档官方口径：股
    assert bar.amount == 10501.0  # turnover → amount（元）


@pytest.mark.parametrize(
    ("adjust", "expected"),
    [(Adjust.NONE, "none"), (Adjust.QFQ, "forward"), (Adjust.HFQ, "backward")],
)
def test_adjust_codes(
    adjust: Adjust, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"item": []}))
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(adjust=adjust)))

    assert _last_call()["params"]["adjust"] == expected


def test_index_bars_uses_index_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """指数走独立端点：无复权语义（params 不含 adjust 键）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"item": bar_rows("date_ms", [START_MS])}))
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-SSE-000001"),
        adjust=Adjust.NONE,
    )
    asyncio.run(adapter.fetch_bars(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/a-share-index/prices/historical"
    assert call["params"]["thscode"] == "000001.SH"
    assert call["params"]["interval"] == "1d"
    assert "adjust" not in call["params"]


def test_etf_bars_uses_fund_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """ETF 走基金行情端点（仅日线、窗口 ≤5 自然年）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"item": bar_rows("date_ms", [START_MS])}))
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.ETF,
        instrument=Instrument.parse("CN-SSE-510300"),
        adjust=Adjust.NONE,
    )
    asyncio.run(adapter.fetch_bars(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/fund/market/historical"
    assert call["params"]["thscode"] == "510300.SH"
    assert "adjust" not in call["params"]


def test_futures_daily_request_and_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """期货日 K：time_period 显式传参；行时间键为 timestamp。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch, envelope({"item": bar_rows("timestamp", [START_MS, END_MS])})
    )
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2601"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(adapter.fetch_bars(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/futures/prices/daily"
    # 端外仅 day_1（显式传参，不依赖上游默认值）
    assert call["params"] == {
        "start": START_MS,
        "end": END_MS,
        "thscode": "CU2601.SHF",
        "time_period": "day_1",
    }
    assert batch.count == 2
    assert batch.bars[0].timestamp == datetime(2026, 1, 1, 0, tzinfo=CN_TIMEZONE)


def test_options_daily_symbol_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """期权合约 symbol 段原样透传（IO2601-C-4000 → IO2601-C-4000.CFE）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"item": bar_rows("timestamp", [START_MS])}))
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.OPTION,
        instrument=Instrument.parse("CN-CFFEX-IO2601-C-4000"),
        adjust=Adjust.NONE,
    )
    asyncio.run(adapter.fetch_bars(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/options/prices/daily"
    assert call["params"]["thscode"] == "IO2601-C-4000.CFE"
    assert call["params"]["time_period"] == "day_1"


def test_closed_interval_filter_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    # 窗口 [2026-01-01, 2026-01-10] 北京时间闭区间：两端点窗内、首尾窗外
    preset_payload(
        monkeypatch,
        envelope(
            {
                "item": bar_rows(
                    "date_ms",
                    [
                        START_MS - 86_400_000,  # 2025-12-31，窗前
                        START_MS,  # 2026-01-01（start 端点，闭区间含）
                        END_MS,  # 2026-01-10（end 端点，闭区间含）
                        END_MS + 86_400_000,  # 2026-01-11，窗外
                    ],
                )
            }
        ),
    )
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query(offset=1, limit=1)))

    assert batch.count == 1
    assert batch.offset == 1
    assert batch.bars[0].timestamp == datetime(2026, 1, 10, 0, tzinfo=CN_TIMEZONE)


@pytest.mark.parametrize("interval", ["P1W", "P1M", "PT1H", "PT1M"])
def test_unsupported_intervals_rejected_by_operation_routing(
    interval: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游历史 K 线端外仅日线：能力目录即不声明，其余周期二级路由拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse(interval)))


def test_unverified_exchange_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """还原表只收文档实证条目：BJ（北交所）价格端点未实证、DCE 期货后缀
    未实证，明确拒绝而非透传上游（写错比透传更隐蔽）。"""
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(
            adapter.fetch_bars(
                _query(instrument=Instrument.parse("CN-BSE-430047"))
            )
        )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(
            adapter.fetch_bars(
                _query(
                    asset_type=AssetType.CONTRACT,
                    instrument=Instrument.parse("CN-DCE-M2601"),
                )
            )
        )


def test_unsupported_asset_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """仅声明 CN 股票/指数/ETF/期货/期权：外汇请求在映射层拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.FOREX,
        instrument=Instrument.parse("GLOBAL-OTC-EUR/USD"),
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


# ---------------------------------------------------------------------------
# 运行前提与错误映射
# ---------------------------------------------------------------------------


def test_missing_api_key() -> None:
    adapter = _adapter(make_settings(ths_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "API key" in str(excinfo.value)


def test_missing_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "httpx", None)  # 模拟依赖未安装
    adapter = _adapter(make_settings(ths_api_key="k"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "httpx" in str(excinfo.value)


def test_envelope_rate_limited_4001(monkeypatch: pytest.MonkeyPatch) -> None:
    """信封 code=4001（频率超限，官方错误码表）：明确映射 UPSTREAM_RATE_LIMITED。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(None, code=4001, message="频率超限"))
    adapter = _adapter()
    with pytest.raises(UpstreamRateLimitedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["upstream_code"] == 4001


def test_envelope_business_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """其余信封非 0 业务码（如 3001 标的不存在）：UPSTREAM_ERROR 携上游码。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(None, code=3001, message="标的不存在"))
    adapter = _adapter()
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["upstream_code"] == 3001
    assert excinfo.value.details["upstream_message"] == "标的不存在"


def test_envelope_data_not_object_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """code=0 但 data 非对象（如 null）：信封契约违背拒绝。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(None))
    adapter = _adapter()
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """官方约定业务错误 HTTP 恒 200：非 2xx 属契约外防御路径（携 status_code）。"""
    install(monkeypatch)
    preset_status(monkeypatch, 500, {"detail": "gateway error"})
    adapter = _adapter()
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["status_code"] == 500


def test_non_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_status(monkeypatch, 200, RuntimeError("bad body"))  # 触发 .json() 解析失败
    adapter = _adapter()
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_bar_missing_timestamp_contract_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bar 行缺时间键属上游契约违背：明确抛 UPSTREAM_ERROR（时间戳是 bar 的
    身份键，loud 失败优于静默丢行）。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch, envelope({"item": [{"open_price": 1.0, "close_price": 1.0}]})
    )
    adapter = _adapter()
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["field"] == "date_ms"


# ---------------------------------------------------------------------------
# 实时快照：A 股 / 指数 / ETF（REST 单次调用语义）
# ---------------------------------------------------------------------------


def test_stock_quote_request_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(snapshot_data(1767225600000, [stock_snapshot_item()])))
    adapter = _adapter()

    quote = asyncio.run(adapter.fetch_quote(_quote_query()))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/a-share/prices/snapshot"
    assert call["params"] == {"thscodes": "600519.SH"}
    assert call["headers"] == {"X-api-key": "k"}

    assert quote.provider == "ths"
    # data.timestamp 毫秒戳 → 统一北京时间
    assert quote.timestamp == datetime(2026, 1, 1, 8, tzinfo=CN_TIMEZONE)
    assert quote.last_price == 1277.8
    assert quote.volume == 3098875.0  # 股（官方口径）
    assert quote.amount == 3937375200.0  # turnover → amount（元）
    # extensions 白名单透出（快照不返回 name，不透出）
    assert quote.extensions == {
        "open_price": 1252.08,
        "high_price": 1282.0,
        "low_price": 1250.21,
        "prev_price": 1256.0,
        "price_change": 21.8,
        "price_change_ratio_pct": 1.735669,
    }
    assert quote.bid_price1 is None  # 上游无盘口字段，防御 null


def test_etf_quote_uses_thscode_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """ETF 快照走单值参数 thscode（非 thscodes），振幅/换手率白名单透出。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        envelope(
            snapshot_data(
                1767225600000,
                [
                    {
                        "thscode": "510300.SH",
                        "ticker": "510300",
                        "last_price": 4.753,
                        "open_price": 4.775,
                        "high_price": 4.825,
                        "low_price": 4.724,
                        "prev_price": 4.838,
                        "price_change_ratio_pct": -1.756924,
                        "price_change": -0.085,
                        "price_amplitude_ratio_pct": 2.08764,
                        "volume": 1657822800,
                        "turnover": 7909234100,
                        "turnover_ratio_pct": 9.012068,
                    }
                ],
            )
        ),
    )
    adapter = _adapter()
    query = _quote_query(asset_type=AssetType.ETF, instrument=Instrument.parse("CN-SSE-510300"))
    quote = asyncio.run(adapter.fetch_quote(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/fund/market/snapshot"
    assert call["params"] == {"thscode": "510300.SH"}
    assert quote.last_price == 4.753
    assert quote.extensions["price_amplitude_ratio_pct"] == 2.08764
    assert quote.extensions["turnover_ratio_pct"] == 9.012068


def test_index_quote_uses_index_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """指数快照走独立端点（thscodes 必填，本网关传单个）。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        envelope(
            snapshot_data(
                1767225600000,
                [
                    {
                        "thscode": "000001.SH",
                        "ticker": "000001",
                        "last_price": 3388.06,
                        "volume": 321000000,
                        "turnover": 420000000000,
                    }
                ],
            )
        ),
    )
    adapter = _adapter()
    query = _quote_query(asset_type=AssetType.INDEX, instrument=Instrument.parse("CN-SSE-000001"))
    quote = asyncio.run(adapter.fetch_quote(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/a-share-index/prices/snapshot"
    assert call["params"] == {"thscodes": "000001.SH"}
    assert quote.last_price == 3388.06


def test_snapshot_null_data_timestamp_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """data.timestamp=null（官方：无有效数据时）属无有效行情，契约违背拒绝。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(snapshot_data(None, [stock_snapshot_item()])))
    adapter = _adapter()
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert excinfo.value.details["field"] == "timestamp"


def test_snapshot_no_last_price_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        envelope(snapshot_data(1767225600000, [{"thscode": "600519.SH"}])),
    )
    adapter = _adapter()
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert excinfo.value.details["field"] == "last_price"


def test_snapshot_empty_item_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游合法无数据返回 item=[]：快照无行情可映射，UPSTREAM_ERROR。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(snapshot_data(1767225600000, [])))
    adapter = _adapter()
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert "quote item" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 实时快照：期货 / 期权（分时端点取末点）
# ---------------------------------------------------------------------------


def test_futures_quote_from_intraday_last_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期货快照经分时端点取末点：防御升序排后取末点 price/timestamp；
    volume/amount=全点求和（当日累计，逐分钟增量口径探针实证）。"""
    install(monkeypatch)
    points = intraday_points(
        [1767232800000, 1767225600000, 1767229200000],  # 乱序入参
        prices=[11.0, 9.0, 10.0],
        volumes=[5.0, 26.0, 31.0],
        turnovers=[100.5, 200.25, 300.75],
    )
    preset_payload(monkeypatch, envelope(intraday_data(1767232800000, points)))
    adapter = _adapter()
    query = _quote_query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2601"),
    )
    quote = asyncio.run(adapter.fetch_quote(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/futures/prices/intraday"
    # session/trade_date 显式传参（端外仅当前交易日）
    assert call["params"] == {
        "thscode": "CU2601.SHF",
        "session": "intraday",
        "trade_date": "0",
    }

    # 末点 = 10:00（北京）价格 11.0
    assert quote.timestamp == datetime(2026, 1, 1, 10, tzinfo=CN_TIMEZONE)
    assert quote.last_price == 11.0
    assert quote.volume == 62.0  # 全点求和（当日累计）
    assert quote.amount == 601.5
    assert quote.extensions is None


def test_intraday_null_timestamp_point_disables_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """官方 item[].timestamp 标明可空：无时间戳的点不参与排序，其量额
    亦不可见——求和序列不完整，累计口径不成立（恒 null 不冒充）。"""
    install(monkeypatch)
    points = intraday_points(
        [1767232800000, 1767225600000, 1767229200000],
        prices=[11.0, 9.0, 10.0],
        volumes=[5.0, 26.0, 31.0],
        turnovers=[100.5, 200.25, 300.75],
    )
    points.append({"timestamp": None, "price": 99.0, "volume": 0, "turnover": 0})
    preset_payload(monkeypatch, envelope(intraday_data(1767232800000, points)))
    adapter = _adapter()
    query = _quote_query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2601"),
    )
    quote = asyncio.run(adapter.fetch_quote(query))

    assert quote.last_price == 11.0
    assert quote.volume is None
    assert quote.amount is None


def test_intraday_missing_volume_point_disables_volume_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """量额两字段独立计算：任一点 volume 缺失则 volume 恒 null（部分和
    不冒充累计），turnover 序列完整则 amount 照常求和；镜像缺失同理。"""
    install(monkeypatch)
    points = intraday_points(
        [1767225600000, 1767229200000],
        prices=[9.0, 10.0],
        volumes=[26.0, 31.0],
        turnovers=[200.25, 300.75],
    )
    points[1]["volume"] = None  # 中段点 volume 缺失（turnover 仍齐备）
    preset_payload(monkeypatch, envelope(intraday_data(1767225600000, points)))
    adapter = _adapter()
    query = _quote_query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2601"),
    )
    quote = asyncio.run(adapter.fetch_quote(query))

    assert quote.volume is None
    assert quote.amount == 501.0

    # 镜像：首点 turnover 缺失（volume 仍齐备）——重建干净序列避免前段污染
    points = intraday_points(
        [1767225600000, 1767229200000],
        prices=[9.0, 10.0],
        volumes=[26.0, 31.0],
        turnovers=[200.25, 300.75],
    )
    points[0]["turnover"] = None
    preset_payload(monkeypatch, envelope(intraday_data(1767225600000, points)))
    quote = asyncio.run(adapter.fetch_quote(query))

    assert quote.volume == 57.0
    assert quote.amount is None


def test_options_quote_from_intraday(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        envelope(intraday_data(1767225600000, intraday_points([1767225600000], prices=[35.2]))),
    )
    adapter = _adapter()
    query = _quote_query(
        asset_type=AssetType.OPTION,
        instrument=Instrument.parse("CN-CFFEX-IO2601-C-4000"),
    )
    quote = asyncio.run(adapter.fetch_quote(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/api/options/prices/intraday"
    assert call["params"]["thscode"] == "IO2601-C-4000.CFE"
    assert quote.last_price == 35.2
    assert quote.volume == 12.0  # 单点序列求和 = 该点值（逐分钟增量）
    assert quote.amount == 4692000.0


def test_intraday_empty_item_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """非交易日/盘前无分时（item=[]）：无有效行情，UPSTREAM_ERROR。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(intraday_data(1767225600000, [])))
    adapter = _adapter()
    query = _quote_query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2601"),
    )
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(query))


def test_intraday_all_null_timestamps_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """全部分时点无时间戳（官方可空）：无有效行情时间，契约违背拒绝。"""
    install(monkeypatch)
    points = [{"timestamp": None, "price": 11.0, "volume": 0, "turnover": 0}]
    preset_payload(monkeypatch, envelope(intraday_data(1767225600000, points)))
    adapter = _adapter()
    query = _quote_query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2601"),
    )
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(query))


# ---------------------------------------------------------------------------
# 快照急切校验
# ---------------------------------------------------------------------------


def test_quote_unsupported_asset_type() -> None:
    """quotes 能力仅声明 CN 五类资产：外汇快照请求在二级路由拒绝。"""
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_quote_operation(
            _quote_query(
                asset_type=AssetType.FOREX,
                instrument=Instrument.parse("GLOBAL-OTC-EUR/USD"),
            )
        )


def test_quote_missing_api_key() -> None:
    adapter = _adapter(make_settings(ths_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert "API key" in str(excinfo.value)
