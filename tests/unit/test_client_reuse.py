"""SDK 客户端实例复用测试（审查项 M4，四家逐家覆盖）。

断言：同 apikey 的连续请求复用同一客户端实例（连接池复用收益的前提）；
``shutdown_runtime`` 关闭并释放引用；关闭后再次请求按需重建。测试桩经
``sys.modules`` 替换 SDK 模块——适配器缓存绑定 SDK 入口身份，桩替换后
缓存自动失效（身份检查的隔离语义由本文件间接验证）。
"""

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from tapehub.domain.models import Adjust, AssetType, BarQuery, DataVariant, Instrument
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tapehub.settings import Settings
from tests.conftest import make_settings
from tests.fixtures.providers.stub_alphafeed import (
    install as install_alphafeed,
)
from tests.fixtures.providers.stub_alphafeed import (
    klines_payload as alphafeed_payload,
)
from tests.fixtures.providers.stub_alphafeed import (
    preset_payload as preset_alphafeed,
)
from tests.fixtures.providers.stub_tickflow import install as install_tickflow
from tests.fixtures.providers.stub_tushare import install as install_tushare
from tests.fixtures.providers.stub_twelvedata import (
    forex_values_rows,
)
from tests.fixtures.providers.stub_twelvedata import (
    install as install_twelvedata,
)
from tests.fixtures.providers.stub_twelvedata import (
    preset_values as preset_twelvedata,
)

CN = ZoneInfo("Asia/Shanghai")


def _stock_query(variant: DataVariant) -> BarQuery:
    from tapehub.domain.intervals import Interval

    return BarQuery(
        asset_type=AssetType.STOCK,
        instrument=Instrument.parse("CN-SSE-600519"),
        interval=Interval.parse("P1D"),
        start=datetime(2026, 1, 1, tzinfo=CN),
        end=datetime(2026, 1, 10, tzinfo=CN),
        adjust=Adjust.QFQ,
        variant=variant,
        offset=0,
        limit=500,
    )


def _forex_query() -> BarQuery:
    from tapehub.domain.intervals import Interval

    return BarQuery(
        asset_type=AssetType.FOREX,
        instrument=Instrument.parse("GLOBAL-OTC-EUR/USD"),
        interval=Interval.parse("P1D"),
        start=datetime(2026, 1, 1, tzinfo=CN),
        end=datetime(2026, 1, 10, tzinfo=CN),
        adjust=Adjust.NONE,
        variant=DataVariant.TWELVEDATA,
        offset=0,
        limit=500,
    )


def _adapter(provider: str, settings: Settings) -> Any:
    module = __import__(
        f"tapehub.providers.{provider}.adapter", fromlist=["ProviderAdapter"]
    )
    manifest = load_builtin_manifest(provider)
    record = build_provider_record(provider, manifest, module.ProviderAdapter, settings=settings)
    return record.adapter


def test_tushare_pro_client_reused(monkeypatch: Any) -> None:
    from tapehub.domain.intervals import Interval

    stub = install_tushare(monkeypatch)
    stub.pro_bar_rows = [
        {
            "ts_code": "600519.SH",
            "trade_date": "20260105",
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.1,
            "vol": 100.0,
            "amount": 1000.0,
        }
    ]
    query = BarQuery(
        asset_type=AssetType.STOCK,
        instrument=Instrument.parse("CN-SSE-600519"),
        interval=Interval.parse("P1D"),
        start=datetime(2026, 1, 1, tzinfo=CN),
        end=datetime(2026, 1, 10, tzinfo=CN),
        adjust=Adjust.QFQ,
        variant=DataVariant.TUSHARE,
    )
    adapter = _adapter("tushare", make_settings(tushare_token="tok"))
    asyncio.run(adapter.fetch_bars(query))
    asyncio.run(adapter.fetch_bars(query))
    # 两次请求仅构造一次 pro 客户端（第二次复用首例）
    assert len(stub.pro_instances) == 1


def test_twelvedata_rest_client_reused(monkeypatch: Any) -> None:
    from tests.fixtures.providers.stub_twelvedata import StubTDClient

    install_twelvedata(monkeypatch)
    preset_twelvedata(monkeypatch, forex_values_rows(["2026-01-05 00:00:00"]))
    adapter = _adapter("twelvedata", make_settings(twelvedata_api_key="tdk"))
    asyncio.run(adapter.fetch_bars(_forex_query()))
    asyncio.run(adapter.fetch_bars(_forex_query()))
    assert len(StubTDClient.instances) == 1
    # 停机释放引用（TDClient 无 close API）；再次请求按需重建
    adapter.shutdown_runtime()
    assert adapter._rest_client is None
    asyncio.run(adapter.fetch_bars(_forex_query()))
    assert len(StubTDClient.instances) == 2


def test_tickflow_client_reused_and_closed_on_shutdown(monkeypatch: Any) -> None:
    clients = install_tickflow(monkeypatch)
    adapter = _adapter("tickflow", make_settings(tickflow_api_key="tfk"))
    asyncio.run(adapter.fetch_bars(_stock_query(DataVariant.TICKFLOW)))
    asyncio.run(adapter.fetch_bars(_stock_query(DataVariant.TICKFLOW)))
    assert len(clients) == 1
    adapter.shutdown_runtime()
    assert clients[0].closed is True  # close 被调（对齐 SDK SyncAPIClient.close）
    asyncio.run(adapter.fetch_bars(_stock_query(DataVariant.TICKFLOW)))
    assert len(clients) == 2  # 释放后按需重建


def test_alphafeed_client_reused_and_closed_on_shutdown(monkeypatch: Any) -> None:
    clients = install_alphafeed(monkeypatch)
    preset_alphafeed(monkeypatch, alphafeed_payload(["2026-01-05"]))
    adapter = _adapter("alphafeed", make_settings(alphafeed_api_key="afk"))
    asyncio.run(adapter.fetch_bars(_stock_query(DataVariant.ALPHAFEED)))
    asyncio.run(adapter.fetch_bars(_stock_query(DataVariant.ALPHAFEED)))
    assert len(clients) == 1
    adapter.shutdown_runtime()
    assert clients[-1].closed is True
    asyncio.run(adapter.fetch_bars(_stock_query(DataVariant.ALPHAFEED)))
    assert len(clients) == 2
