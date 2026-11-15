"""Tushare 适配器单元测试：请求映射、响应映射与运行前提（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tapehub.domain.errors import ProviderNotImplementedError, UnsupportedCapabilityError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, DataVariant, Instrument
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tapehub.settings import Settings
from tests.conftest import make_settings
from tests.fixtures.providers.stub_tushare import (
    fund_rows,
    futures_rows,
    install,
    option_rows,
    stock_rows,
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
        "variant": DataVariant.TUSHARE,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _adapter(settings: Settings | None = None) -> Any:
    from tapehub.providers.tushare.adapter import ProviderAdapter

    manifest = load_builtin_manifest("tushare")
    record = build_provider_record(
        "tushare", manifest, ProviderAdapter, settings=settings or make_settings(tushare_token="k")
    )
    return record.adapter


def test_stock_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2026-01-05", "2026-01-06"])
    adapter = _adapter(make_settings(tushare_token="test-token"))

    batch = asyncio.run(adapter.fetch_bars(_query()))

    pro = stub.last_pro
    assert pro.token == "test-token"  # settings 注入链路
    kwargs = dict(stub.pro_bar_calls[0])
    passed_api = kwargs.pop("api")
    assert passed_api is pro  # pro 客户端经 api 参数显式传入
    # 其余参数全部显式传递；asset='E' 为官方枚举
    assert kwargs == {
        "ts_code": "600000.SH",
        "asset": "E",
        "freq": "D",
        "adj": "qfq",  # 股票默认 qfq 显式传递（上游默认 None 未复权）
        "start_date": "20260101",
        "end_date": "20260110",
    }

    assert batch.provider == "tushare"
    assert batch.partial is False
    assert batch.count == 2
    bar = batch.bars[0]
    assert bar.timestamp == datetime(2026, 1, 5, tzinfo=CN)
    assert bar.close == 10.2
    # 单位换算：vol 手→股（×100）、amount 千元→元（×1000）
    assert bar.volume == 1234500.0
    assert bar.amount == 12345000.0
    extensions = bar.extensions
    assert extensions is not None
    assert extensions["pre_close"] == 10.1
    assert extensions["pct_chg"] == 1.0


def test_bse_stock_request_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """北交所 BSE→BJ 后缀映射（探针实证：目录 355 在市标的全部 920 段
    + 920000.BJ daily/adj_factor 双证）。"""
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2026-01-05"])
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(instrument=Instrument.parse("CN-BSE-920000"))))
    assert stub.pro_bar_calls[0]["ts_code"] == "920000.BJ"


@pytest.mark.parametrize(
    ("interval", "expected"),
    [("P1D", "D"), ("P1W", "W"), ("P1M", "M")],
)
def test_freq_codes(interval: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2026-01-05"])
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse(interval))))
    assert stub.pro_bar_calls[0]["freq"] == expected


def test_adjust_none_maps_to_python_none(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2026-01-05"])
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(adjust=Adjust.NONE)))
    assert stub.pro_bar_calls[0]["adj"] is None  # 上游枚举：None=未复权


@pytest.mark.parametrize("interval", ["P1W", "P1M"])
def test_stock_weekly_monthly_units_passthrough(
    interval: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """周/月线上游实数据已是股/元（同窗求和互证：官方文档声明「手/千元」
    与实数据矛盾，比率精确 ×100/×1000），如实透传不再换算——此前统一
    ×100/×1000 为 100/1000 倍错误，本用例锁定回归。"""
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2026-01-05"])
    adapter = _adapter()
    batch = asyncio.run(
        adapter.fetch_bars(_query(interval=Interval.parse(interval), adjust=Adjust.NONE))
    )
    bar = batch.bars[0]
    assert bar.volume == 12345.0  # 上游 vol 已是股，透传
    assert bar.amount == 12345.0  # 上游 amount 已是元，透传


def test_index_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """指数经 pro_bar(asset='I')：ts_code/asset 按资产类型显式传递，量额
    换算与股票日线同口径（指数日线 vol 手/amount 千元，doc_id=95 定案）。"""
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2026-01-05"])
    adapter = _adapter()
    batch = asyncio.run(
        adapter.fetch_bars(
            _query(
                asset_type=AssetType.INDEX,
                instrument=Instrument.parse("CN-SSE-000001"),
                adjust=Adjust.NONE,
            )
        )
    )
    kwargs = dict(stub.pro_bar_calls[0])
    passed_api = kwargs.pop("api")
    assert passed_api is stub.last_pro  # pro 客户端经 api 参数显式传入
    assert kwargs == {
        "ts_code": "000001.SH",
        "asset": "I",
        "freq": "D",
        "adj": None,  # 指数无复权（官方 adj「只针对股票」）
        "start_date": "20260101",
        "end_date": "20260110",
    }
    bar = batch.bars[0]
    assert bar.close == 10.2
    # 日线口径：vol 手→股（×100）、amount 千元→元（×1000）
    assert bar.volume == 1234500.0
    assert bar.amount == 12345000.0
    extensions = bar.extensions
    assert extensions is not None
    assert extensions["pre_close"] == 10.1
    assert extensions["pct_chg"] == 1.0


def test_index_szse_ts_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """深市指数 SZ→SZ 后缀（399 段在 resolver 直接定型 INDEX）。"""
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2026-01-05"])
    adapter = _adapter()
    asyncio.run(
        adapter.fetch_bars(
            _query(
                asset_type=AssetType.INDEX,
                instrument=Instrument.parse("CN-SZSE-399001"),
                adjust=Adjust.NONE,
            )
        )
    )
    kwargs = stub.pro_bar_calls[0]
    assert kwargs["ts_code"] == "399001.SZ"
    assert kwargs["asset"] == "I"


def test_unsupported_index_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """指数拒绝范围 {SSE, SZSE}：BSE 指数不在官方「沪深指数」枚举内
    （doc_id=109），不声明、映射层明确拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-BSE-899050"),
        adjust=Adjust.NONE,
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_etf_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """ETF 经 fund_daily（仅日线）：量额口径与股票日线同（doc_id=127 定案）。"""
    stub = install(monkeypatch)
    stub.set_rows("fund_daily", fund_rows(["2026-01-05"]))
    adapter = _adapter()

    query = _query(
        asset_type=AssetType.ETF,
        instrument=Instrument.parse("CN-SSE-510050"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(adapter.fetch_bars(query))

    operation, kwargs = stub.last_pro.calls[0]
    assert operation == "fund_daily"
    assert kwargs == {
        "ts_code": "510050.SH",
        "start_date": "20260101",
        "end_date": "20260110",
    }
    bar = batch.bars[0]
    # 日线口径：vol 手→股（×100）、amount 千元→元（×1000）
    assert bar.volume == 735136118.0
    assert bar.amount == 2068149774.0
    assert bar.extensions is not None
    assert bar.extensions["pre_close"] == 2.79
    assert bar.extensions["pct_chg"] == 0.179


def test_etf_szse_ts_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """深市 ETF SZ→SZ 后缀（fund_daily 沪深同构，探针实证 159915.SZ）。"""
    stub = install(monkeypatch)
    stub.set_rows("fund_daily", fund_rows(["2026-01-05"]))
    adapter = _adapter()
    asyncio.run(
        adapter.fetch_bars(
            _query(
                asset_type=AssetType.ETF,
                instrument=Instrument.parse("CN-SZSE-159915"),
                adjust=Adjust.NONE,
            )
        )
    )
    assert stub.last_pro.calls[0][1]["ts_code"] == "159915.SZ"


def test_option_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """期权经 opt_daily（仅日线）：合约符号原样透传 + 后缀直拼，amount
    万元→元（与期货同款口径），extensions settle/oi。"""
    stub = install(monkeypatch)
    stub.set_rows("opt_daily", option_rows(["2026-01-05"]))
    adapter = _adapter()

    query = _query(
        asset_type=AssetType.OPTION,
        instrument=Instrument.parse("CN-CFFEX-IO2611-C-4150"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(adapter.fetch_bars(query))

    operation, kwargs = stub.last_pro.calls[0]
    assert operation == "opt_daily"
    assert kwargs == {
        "ts_code": "IO2611-C-4150.CFX",  # CFFEX 期权 .CFX 与该所期货同后缀
        "start_date": "20260101",
        "end_date": "20260110",
    }
    bar = batch.bars[0]
    assert bar.volume == 18942.0  # 手（张）保持
    assert bar.amount == 17370330.0  # 万元→元（×10000）
    assert bar.extensions is not None
    assert bar.extensions["settle"] == 0.1139
    assert bar.extensions["oi"] == 11987.0


def test_unsupported_etf_and_option_exchanges(monkeypatch: pytest.MonkeyPatch) -> None:
    """ETF 拒绝范围 {SSE, SZSE}、期权拒绝范围六所之外：映射层明确拒绝
    （广期所期权上游枚举未含，绝不预填未核实条目）。"""
    install(monkeypatch)
    adapter = _adapter()
    for asset_type, code in (
        (AssetType.ETF, "CN-BSE-920000"),
        (AssetType.OPTION, "CN-GFEX-LC2601"),
    ):
        query = _query(asset_type=asset_type, instrument=Instrument.parse(code), adjust=Adjust.NONE)
        with pytest.raises(UnsupportedCapabilityError):
            asyncio.run(adapter.fetch_bars(query))


def test_futures_request_and_response_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install(monkeypatch)
    stub.set_rows("fut_daily", futures_rows(["2026-01-05"]))
    adapter = _adapter()

    query = _query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2401"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(adapter.fetch_bars(query))

    pro = stub.last_pro
    operation, kwargs = pro.calls[0]
    assert operation == "fut_daily"
    assert kwargs == {
        "ts_code": "CU2401.SHF",  # 期货后缀已按文档示例核实（上期所）
        "start_date": "20260101",
        "end_date": "20260110",
    }

    bar = batch.bars[0]
    assert bar.timestamp == datetime(2026, 1, 5, tzinfo=CN)
    assert bar.close == 68300.0
    # 期货成交量保持手；amount 万元→元（×10000）
    assert bar.volume == 54321.0
    assert bar.amount == 371000000.0
    assert bar.extensions is not None
    assert bar.extensions["settle"] == 68250.0
    assert bar.extensions["oi"] == 100000.0


def test_futures_partial_on_row_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install(monkeypatch)
    stub.set_rows("fut_daily", futures_rows(["2026-01-05"]) * 2000)
    adapter = _adapter()

    query = _query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SHFE-CU2401"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(adapter.fetch_bars(query))
    assert batch.partial is True  # 达到 fut_daily 单次 2000 条上限


@pytest.mark.parametrize(
    ("code", "asset_type", "expected"),
    [
        # 五所期货后缀（探针实证）
        ("CN-SHFE-CU2401", AssetType.CONTRACT, "CU2401.SHF"),
        ("CN-DCE-V2601", AssetType.CONTRACT, "V2601.DCE"),
        ("CN-INE-SC2409", AssetType.CONTRACT, "SC2409.INE"),
        ("CN-CFFEX-IF2409", AssetType.CONTRACT, "IF2409.CFX"),
        # GFEX：.GFE 后缀（2026-11-16 探针实证 LC2706.GFE 有数据、fut_basic
        # GFEX 在市 154 行）
        ("CN-GFEX-LC2706", AssetType.CONTRACT, "LC2706.GFE"),
        # CZCE 与统一标准码同为 4 位年月，直通（探针实证 SR2601.ZCE 有数据、
        # 3 位旧写法全空）
        ("CN-CZCE-RM2701", AssetType.CONTRACT, "RM2701.ZCE"),
        # 3 位旧写法输入不做年份猜测，如实透传给上游（查空如实表现）
        ("CN-CZCE-SR601", AssetType.CONTRACT, "SR601.ZCE"),
        # 主力连续：多数交易所品种+L（XXL 主连），CFFEX 主力直拼（探针实证）
        ("CN-SHFE-CU", AssetType.CONTINUOUS, "CUL.SHF"),
        ("CN-CZCE-SR", AssetType.CONTINUOUS, "SRL.ZCE"),
        ("CN-DCE-M", AssetType.CONTINUOUS, "ML.DCE"),
        ("CN-INE-SC", AssetType.CONTINUOUS, "SCL.INE"),
        ("CN-CFFEX-IF", AssetType.CONTINUOUS, "IF.CFX"),
        # GFEX 主连品种+L 同构（2026-11-16 探针实证 LCL.GFE 5 行真实数据）
        ("CN-GFEX-LC", AssetType.CONTINUOUS, "LCL.GFE"),
    ],
)
def test_futures_ts_code_mapping(
    code: str,
    asset_type: AssetType,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = install(monkeypatch)
    stub.set_rows("fut_daily", futures_rows(["2026-01-05"]))
    adapter = _adapter()

    query = _query(asset_type=asset_type, instrument=Instrument.parse(code), adjust=Adjust.NONE)
    asyncio.run(adapter.fetch_bars(query))

    operation, kwargs = stub.last_pro.calls[0]
    assert operation == "fut_daily"
    assert kwargs["ts_code"] == expected


def test_unsupported_futures_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    """六所期货交易所（SHFE/DCE/INE/CFFEX/CZCE/GFEX）之外的 exchange 段在
    映射层明确拒绝（如股票交易所段），不透传上游（写错比透传更隐蔽）。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-SSE-CU2401"),
        adjust=Adjust.NONE,
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_czce_irregular_symbol_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """CZCE 不规则合约代码（位数不足等）不做映射层拒绝或年份猜测，
    如实透传给上游——上游查空/报错即最诚实的行为表现。"""
    stub = install(monkeypatch)
    stub.set_rows("fut_daily", futures_rows(["2026-01-05"]))
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-CZCE-SR60"),
        adjust=Adjust.NONE,
    )
    asyncio.run(adapter.fetch_bars(query))
    _, kwargs = stub.last_pro.calls[0]
    assert kwargs["ts_code"] == "SR60.ZCE"


def test_unsupported_stock_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    # CN 股票接口不接港股（HKEX 不在交易所白名单内）：合法标准代码由
    # 映射层拒绝（market/exchange 矛盾组合由域层词汇表拦截）
    install(monkeypatch)
    adapter = _adapter()
    query = _query(instrument=Instrument.parse("HK-HKEX-00700"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_missing_token() -> None:
    adapter = _adapter(make_settings(tushare_token=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "token" in str(excinfo.value).lower()


def test_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tushare", None)  # import 失败，模拟 SDK 未安装
    adapter = _adapter(make_settings(tushare_token="k"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "not installed" in str(excinfo.value)


def test_closed_interval_filter_and_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = install(monkeypatch)
    stub.pro_bar_rows = stock_rows(["2025-12-31", "2026-01-02", "2026-01-03", "2026-02-01"])
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query(offset=1, limit=1)))

    assert batch.count == 1
    assert batch.offset == 1
    assert batch.bars[0].timestamp == datetime(2026, 1, 3, tzinfo=CN)
