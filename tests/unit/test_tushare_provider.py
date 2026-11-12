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
from tests.fixtures.providers.stub_tushare import futures_rows, install, stock_rows

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
        # 五所期货后缀（探针实证，报告 20261112T124404+0800）
        ("CN-SHFE-CU2401", AssetType.CONTRACT, "CU2401.SHF"),
        ("CN-DCE-V2601", AssetType.CONTRACT, "V2601.DCE"),
        ("CN-INE-SC2409", AssetType.CONTRACT, "SC2409.INE"),
        ("CN-CFFEX-IF2409", AssetType.CONTRACT, "IF2409.CFX"),
        # CZCE 原生 3 位年月码 → tushare 4 位（10 年窗口唯一解析，探针实证
        # SR2601.ZCE 有数据）
        ("CN-CZCE-SR601", AssetType.CONTRACT, "SR2601.ZCE"),
        ("CN-CZCE-SR509", AssetType.CONTRACT, "SR2509.ZCE"),
        # 主力连续：多数交易所品种+L（XXL 主连），CFFEX 主力直拼
        # （探针实证，报告 20261112T133250+0800）
        ("CN-SHFE-CU", AssetType.CONTINUOUS, "CUL.SHF"),
        ("CN-CZCE-SR", AssetType.CONTINUOUS, "SRL.ZCE"),
        ("CN-DCE-M", AssetType.CONTINUOUS, "ML.DCE"),
        ("CN-INE-SC", AssetType.CONTINUOUS, "SCL.INE"),
        ("CN-CFFEX-IF", AssetType.CONTINUOUS, "IF.CFX"),
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
    """未接入的期货交易所在映射层明确拒绝（广期所 .GFE 未探针核实，证伪即移除）。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-GFEX-LC2601"),
        adjust=Adjust.NONE,
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_czce_invalid_month_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """CZCE 非 3 位年月码在转换层明确拒绝（如位数不足或含非数字段）。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CONTRACT,
        instrument=Instrument.parse("CN-CZCE-SR60"),
        adjust=Adjust.NONE,
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_unsupported_stock_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    adapter = _adapter()
    query = _query(instrument=Instrument.parse("CN-HKEX-00700"))
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
