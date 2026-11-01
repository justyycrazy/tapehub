"""akshare 适配器单元测试：请求映射、响应映射与延迟导入（SDK 全部 Mock）。"""

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any

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
    em_stock_rows,
    sina_futures_rows,
    sina_index_rows,
    stock_rows_en,
    tx_index_rows,
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
    assert batch.bars[0].timestamp == datetime(2026, 1, 1, tzinfo=UTC)
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
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 5, tzinfo=UTC),
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
