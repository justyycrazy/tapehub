"""akshare 适配器单元测试：请求映射、响应映射与延迟导入（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    UnsupportedCapabilityError,
)
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, DataVariant, Instrument
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tests.fixtures.providers.stub_akshare import (
    StubAkshare,
    em_etf_rows,
    em_global_rows,
    em_index_rows,
    em_stock_rows,
    sina_futures_rows,
    sina_index_rows,
    stock_rows_en,
    tx_index_rows,
)

# CN 标的的时间表达统一用北京时间（领域层会将边界归一化到市场规范时区）
CN = ZoneInfo("Asia/Shanghai")


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN),
        "end": datetime(2026, 1, 10, tzinfo=CN),
        "adjust": Adjust.QFQ,
        "variant": DataVariant.EASTMONEY,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


@pytest.fixture()
def akshare_adapter() -> Any:
    manifest = load_builtin_manifest("akshare")
    return build_provider_record("akshare", manifest, _adapter_cls()).adapter


def _adapter_cls() -> type:
    from tapehub.providers.akshare.adapter import ProviderAdapter

    return ProviderAdapter


def _install_stub(monkeypatch: pytest.MonkeyPatch) -> StubAkshare:
    stub = StubAkshare()
    monkeypatch.setitem(sys.modules, "akshare", stub)
    return stub


def test_em_stock_request_and_response_mapping(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_zh_a_hist", em_stock_rows(["2026-01-01", "2026-01-02"]))

    batch = asyncio.run(akshare_adapter.fetch_bars(_query()))

    operation, kwargs = stub.calls[0]
    assert operation == "stock_zh_a_hist"
    # 所有参数显式传递，不依赖上游默认值
    assert kwargs == {
        "symbol": "600000",
        "period": "daily",
        "start_date": "20260101",
        "end_date": "20260110",
        "adjust": "qfq",
    }
    assert batch.provider == "akshare"
    assert batch.count == 2
    assert batch.bars[0].timestamp == datetime(2026, 1, 1, tzinfo=CN)
    assert batch.bars[0].close == 10.2
    # 东财成交量单位为手，标准化为股（×100）
    assert batch.bars[0].volume == 1234500.0
    assert batch.bars[0].amount == 12345678.0
    extensions = batch.bars[0].extensions
    assert extensions is not None
    assert extensions["pct_change"] == 2.0
    assert extensions["turnover_rate"] == 1.5


def test_sina_stock_symbol_prefix(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_zh_a_daily", stock_rows_en(["2026-01-01"]))

    asyncio.run(akshare_adapter.fetch_bars(_query(variant=DataVariant.SINA)))

    _, kwargs = stub.calls[0]
    assert kwargs["symbol"] == "sh600000"
    assert kwargs["adjust"] == "qfq"


def test_tencent_stock_symbol(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_zh_a_hist_tx", stock_rows_en(["2026-01-01"]))

    batch = asyncio.run(akshare_adapter.fetch_bars(_query(variant=DataVariant.TENCENT)))

    operation, kwargs = stub.calls[0]
    assert operation == "stock_zh_a_hist_tx"
    assert kwargs["symbol"] == "sh600000"
    assert kwargs["adjust"] == "qfq"
    # 新浪/腾讯成交量单位为股，不做换算
    assert batch.bars[0].volume == 1234500.0


def test_em_stock_weekly_period(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_zh_a_hist", em_stock_rows(["2026-01-05"]))

    asyncio.run(akshare_adapter.fetch_bars(_query(interval=Interval.parse("P1W"))))

    _, kwargs = stub.calls[0]
    assert kwargs["period"] == "weekly"


def test_etf_request_and_response_mapping(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("fund_etf_hist_em", em_etf_rows(["2026-01-01", "2026-01-02"]))

    query = _query(
        asset_type=AssetType.ETF,
        instrument=Instrument.parse("CN-SSE-510050"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(akshare_adapter.fetch_bars(query))

    operation, kwargs = stub.calls[0]
    assert operation == "fund_etf_hist_em"
    # ETF 与个股东财接口同参数形状；symbol 为纯 6 位代码，所有参数显式传递
    assert kwargs == {
        "symbol": "510050",
        "period": "daily",
        "start_date": "20260101",
        "end_date": "20260110",
        "adjust": "",
    }
    assert batch.bars[0].close == 3.15
    # 成交量单位为手，标准化为股（×100）
    assert batch.bars[0].volume == 2345600.0
    extensions = batch.bars[0].extensions
    assert extensions is not None
    assert extensions["pct_change"] == 1.6
    assert extensions["turnover_rate"] == 0.9


def test_etf_szse_symbol(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 深市 ETF（1 开头）：纯 6 位代码透传，上游按代码首位自动判断沪深
    stub = _install_stub(monkeypatch)
    stub.set_rows("fund_etf_hist_em", em_etf_rows(["2026-01-02"]))

    query = _query(
        asset_type=AssetType.ETF,
        instrument=Instrument.parse("CN-SZSE-159915"),
        adjust=Adjust.QFQ,
    )
    asyncio.run(akshare_adapter.fetch_bars(query))

    _, kwargs = stub.calls[0]
    assert kwargs["symbol"] == "159915"
    assert kwargs["adjust"] == "qfq"


def test_etf_minute_capability_boundary(
    akshare_adapter: Any,
) -> None:
    # ETF 分钟族未声明（fund_etf_hist_min_em 不接入），应在二级路由被拒绝
    query = _query(
        asset_type=AssetType.ETF,
        instrument=Instrument.parse("CN-SSE-510050"),
        interval=Interval.parse("PT5M"),
        adjust=Adjust.NONE,
    )
    with pytest.raises(UnsupportedCapabilityError):
        akshare_adapter.resolve_operation(query)


def test_hk_request_and_response_mapping(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_hk_hist", em_global_rows(["2026-01-02"]))

    query = _query(
        instrument=Instrument.parse("HK-HKEX-00700"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(akshare_adapter.fetch_bars(query))

    operation, kwargs = stub.calls[0]
    assert operation == "stock_hk_hist"
    # 所有参数显式传递；日期参数上游忽略（全量拉取客户端切片）仍显式传
    assert kwargs == {
        "symbol": "00700",
        "period": "daily",
        "start_date": "20260101",
        "end_date": "20260110",
        "adjust": "",
    }
    assert batch.bars[0].timestamp == datetime(2026, 1, 2, tzinfo=CN)  # 港股=北京零点
    assert batch.bars[0].close == 88.5
    # 成交量单位未核实，原样透出不换算
    assert batch.bars[0].volume == 345678.0


def test_hk_short_code_zero_filled(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 显式短代码（绕过解析器归一）防御性零填充到 5 位
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_hk_hist", em_global_rows(["2026-01-02"]))

    query = _query(
        instrument=Instrument.parse("HK-HKEX-700"),
        adjust=Adjust.QFQ,
    )
    asyncio.run(akshare_adapter.fetch_bars(query))

    _, kwargs = stub.calls[0]
    assert kwargs["symbol"] == "00700"
    assert kwargs["adjust"] == "qfq"


def test_us_symbol_market_codes(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 东财市场号映射：NASDAQ→105、NYSE→106；ticker 大写归一
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_us_hist", em_global_rows(["2026-01-02"]))

    for instrument, expected in [
        ("US-NASDAQ-AAPL", "105.AAPL"),
        ("US-NYSE-TTE", "106.TTE"),
    ]:
        query = _query(
            instrument=Instrument.parse(instrument),
            adjust=Adjust.QFQ,
        )
        asyncio.run(akshare_adapter.fetch_bars(query))
        _, kwargs = stub.calls[-1]
        assert kwargs["symbol"] == expected
        assert kwargs["adjust"] == "qfq"


def test_us_timestamp_new_york_midnight(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 美股日线为美东零点口径（对齐 twelvedata）：EST→北京 13:00、EDT→北京 12:00
    stub = _install_stub(monkeypatch)
    stub.set_rows(
        "stock_us_hist",
        em_global_rows(["2026-01-02", "2026-07-01"]),
    )

    query = _query(
        instrument=Instrument.parse("US-NYSE-GE"),
        start=datetime(2026, 1, 1, tzinfo=CN),
        end=datetime(2026, 7, 10, tzinfo=CN),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(akshare_adapter.fetch_bars(query))

    assert [bar.timestamp for bar in batch.bars] == [
        datetime(2026, 1, 2, 13, tzinfo=CN),  # EST（UTC-5）
        datetime(2026, 7, 1, 12, tzinfo=CN),  # EDT（UTC-4）
    ]
    # 成交量原样透出不换算
    assert batch.bars[0].volume == 345678.0


def test_us_cboe_rejected(akshare_adapter: Any) -> None:
    # CBOE 在东财的市场号归属未核实，不映射不声明
    query = _query(
        instrument=Instrument.parse("US-CBOE-BABA"),
        adjust=Adjust.NONE,
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(akshare_adapter.fetch_bars(query))


def test_interval_capability_boundary(
    akshare_adapter: Any,
) -> None:
    # 新浪个股仅支持日线；周线应在二级路由被拒绝
    with pytest.raises(UnsupportedCapabilityError):
        akshare_adapter.resolve_operation(
            _query(variant=DataVariant.SINA, interval=Interval.parse("P1W"))
        )


def test_index_mapping(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_zh_index_daily", sina_index_rows(["2026-01-02"]))

    query = _query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-SSE-000001"),
        adjust=Adjust.NONE,
        variant=DataVariant.SINA,
    )
    batch = asyncio.run(akshare_adapter.fetch_bars(query))

    operation, kwargs = stub.calls[0]
    assert operation == "stock_zh_index_daily"
    assert kwargs == {"symbol": "sh000001"}  # 新浪指数接口无日期参数
    bar = batch.bars[0]
    assert bar.close == 3005.0
    assert bar.amount is None  # 新浪指数无成交额字段
    assert bar.volume == 1.0


def test_tencent_index_amount_is_volume(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("stock_zh_index_daily_tx", tx_index_rows(["2026-01-02"]))

    query = _query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-SSE-000001"),
        adjust=Adjust.NONE,
        variant=DataVariant.TENCENT,
    )
    batch = asyncio.run(akshare_adapter.fetch_bars(query))

    bar = batch.bars[0]
    assert bar.volume == 999.0  # 腾讯指数 amount 列语义为成交量
    assert bar.amount is None


def test_em_index_request_and_response_mapping(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("index_zh_a_hist", em_index_rows(["2026-01-01", "2026-01-02"]))

    query = _query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-SSE-000001"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(akshare_adapter.fetch_bars(query))

    operation, kwargs = stub.calls[0]
    assert operation == "index_zh_a_hist"
    # 指数东财与个股东财同参数形状；symbol 为纯 6 位代码，无复权参数
    assert kwargs == {
        "symbol": "000001",
        "period": "daily",
        "start_date": "20260101",
        "end_date": "20260110",
    }
    assert batch.bars[0].close == 3262.56
    # 成交量单位为手，原样透出不换算（与新浪/腾讯指数口径一致）
    assert batch.bars[0].volume == 561375.0
    assert batch.bars[0].amount == 603340526739.8
    extensions = batch.bars[0].extensions
    assert extensions is not None
    assert extensions["pct_change"] == -1.58
    assert extensions["turnover_rate"] == 0.0


def test_em_index_weekly_monthly_period(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("index_zh_a_hist", em_index_rows(["2026-01-09"]))

    asyncio.run(akshare_adapter.fetch_bars(_query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-SSE-000001"),
        adjust=Adjust.NONE,
        interval=Interval.parse("P1W"),
    )))
    asyncio.run(akshare_adapter.fetch_bars(_query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-SSE-000001"),
        adjust=Adjust.NONE,
        interval=Interval.parse("P1M"),
    )))

    assert stub.calls[0][1]["period"] == "weekly"
    assert stub.calls[1][1]["period"] == "monthly"


def test_em_index_szse_csi_bse_symbol(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 指数东财为纯 6 位代码，上游按指数代码映射表判定市场；
    # 交易所覆盖与旧前缀式接口一致（SSE/SZSE/CSI/BSE，探针实测）
    stub = _install_stub(monkeypatch)
    stub.set_rows("index_zh_a_hist", em_index_rows(["2026-01-02"]))

    for instrument in ("CN-SZSE-399001", "CN-CSI-000905", "CN-BSE-899050"):
        asyncio.run(akshare_adapter.fetch_bars(_query(
            asset_type=AssetType.INDEX,
            instrument=Instrument.parse(instrument),
            adjust=Adjust.NONE,
        )))
        _, kwargs = stub.calls[-1]
        assert kwargs["symbol"] == instrument.split("-")[-1]


def test_futures_continuous_symbol(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows("futures_zh_daily_sina", sina_futures_rows(["2026-01-02"]))

    query = _query(
        asset_type=AssetType.CONTINUOUS,
        instrument=Instrument.parse("CN-SHFE-RB"),
        adjust=Adjust.NONE,
        variant=DataVariant.SINA,
    )
    batch = asyncio.run(akshare_adapter.fetch_bars(query))

    operation, kwargs = stub.calls[0]
    assert operation == "futures_zh_daily_sina"
    assert kwargs == {"symbol": "RB0"}  # 主力连续 = 品种代码 + 0
    bar = batch.bars[0]
    assert bar.close == 4005.0
    assert bar.extensions is not None
    assert bar.extensions["hold"] == 100000.0
    assert bar.extensions["settle"] == 4004.0


def test_closed_interval_filter(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    # 上游可能返回窗口外的日期行，适配器必须按 [start, end] 精确过滤
    stub.set_rows(
        "stock_zh_a_hist",
        em_stock_rows(["2025-12-31", "2026-01-01", "2026-01-05", "2026-02-01"]),
    )

    batch = asyncio.run(akshare_adapter.fetch_bars(_query()))

    timestamps = [bar.timestamp for bar in batch.bars]
    assert timestamps == [
        datetime(2026, 1, 1, tzinfo=CN),
        datetime(2026, 1, 5, tzinfo=CN),
    ]


def test_pagination(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch)
    stub.set_rows(
        "stock_zh_a_hist",
        em_stock_rows(["2026-01-0%d" % d for d in range(1, 10)]),
    )

    batch = asyncio.run(akshare_adapter.fetch_bars(_query(offset=2, limit=3)))

    assert batch.offset == 2
    assert batch.limit == 3
    assert batch.count == 3
    assert [bar.timestamp.day for bar in batch.bars] == [3, 4, 5]


def test_missing_sdk_returns_not_implemented(
    akshare_adapter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # sys.modules 中置 None 使 import 失败，模拟 SDK 未安装
    monkeypatch.setitem(sys.modules, "akshare", None)
    with pytest.raises(ProviderNotImplementedError):
        asyncio.run(akshare_adapter.fetch_bars(_query()))


def test_unsupported_exchange_rejected(akshare_adapter: Any) -> None:
    # 证券代码不在已核实的交易所集合内
    query = _query(instrument=Instrument.parse("CN-HKEX-00700"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(akshare_adapter.fetch_bars(query))
