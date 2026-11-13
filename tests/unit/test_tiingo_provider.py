"""Tiingo 适配器单元测试：请求映射、响应映射与运行前提（HTTP/WS 全部 Mock）。"""

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
from tapehub.providers.tiingo.request_mapper import REST_BASE
from tests.conftest import make_settings
from tests.fixtures.providers.stub_tiingo import (
    crypto_rows,
    forex_rows,
    install,
    preset_payload,
    preset_status,
)


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.FOREX,
        "instrument": Instrument.parse("GLOBAL-OTC-EUR/USD"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.NONE,
        "variant": DataVariant.TIINGO,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _quote_query(**overrides: Any) -> QuoteQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.FOREX,
        "instrument": Instrument.parse("GLOBAL-OTC-EUR/USD"),
        "variant": DataVariant.TIINGO,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def _adapter(settings: Any = None) -> Any:
    from tapehub.providers.tiingo.adapter import ProviderAdapter

    manifest = load_builtin_manifest("tiingo")
    record = build_provider_record(
        "tiingo",
        manifest,
        ProviderAdapter,
        settings=settings or make_settings(tiingo_api_key="k"),
    )
    return record.adapter


def _last_call() -> dict[str, Any]:
    from tests.fixtures.providers.stub_tiingo import StubClient

    return StubClient.instances[-1].calls[-1]


def test_forex_request_and_response_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        forex_rows(["2026-01-01T00:00:00.000Z", "2026-01-02T00:00:00.000Z"]),
    )
    adapter = _adapter(make_settings(tiingo_api_key="test-key"))

    batch = asyncio.run(adapter.fetch_bars(_query()))

    call = _last_call()
    # 端点按 operation 分流：外汇为每 ticker 独立路径；认证经 Authorization
    # 头（token 不落 URL）；时间边界显式传 UTC 墙钟（北京 1 月 1 日零点 =
    # UTC 2025-12-31 16:00），resampleFreq 显式传（不依赖上游默认值）
    assert call == {
        "url": f"{REST_BASE}/tiingo/fx/eurusd/prices",
        "params": {
            "startDate": "2025-12-31T16:00:00",
            "endDate": "2026-01-09T16:00:00",
            "resampleFreq": "1day",
        },
        "headers": {"Authorization": "Token test-key"},
    }

    assert batch.provider == "tiingo"
    assert batch.partial is False  # 上游无文档化行数上限，不编造截断结论
    assert batch.count == 2
    bar = batch.bars[0]
    # date 为 ISO UTC（官方口径）→ 统一北京时间：UTC 00:00 = 北京 08:00
    assert bar.timestamp == datetime(2026, 1, 1, 8, tzinfo=CN_TIMEZONE)
    assert bar.close == 1.0855
    assert bar.volume is None  # 外汇文档仅 OHLC，无 volume
    assert bar.amount is None  # 上游无成交额字段


def test_crypto_request_tickers_and_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """加密端点走查询参数 tickers；volume 有值（base 币计）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, crypto_rows(["2026-01-01T00:00:00.000Z"]))
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CRYPTO,
        instrument=Instrument.parse("GLOBAL-CRYPTO-BTC/USD"),
    )
    batch = asyncio.run(adapter.fetch_bars(query))

    call = _last_call()
    assert call["url"] == f"{REST_BASE}/tiingo/crypto/prices"
    assert call["params"]["tickers"] == "btcusd"  # 小写去斜杠
    assert call["params"]["resampleFreq"] == "1day"

    bar = batch.bars[0]
    assert bar.timestamp == datetime(2026, 1, 1, 8, tzinfo=CN_TIMEZONE)
    assert bar.volume == 12345.6  # 加密 volume 有值
    assert bar.amount is None


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("PT1M", "1min"),
        ("PT5M", "5min"),
        ("PT15M", "15min"),
        ("PT30M", "30min"),
        ("PT1H", "1hour"),
        ("P1D", "1day"),
    ],
)
def test_interval_codes(
    interval: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, [])
    adapter = _adapter()
    asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse(interval))))

    assert _last_call()["params"]["resampleFreq"] == expected


def test_closed_interval_filter_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    # 窗口 [2026-01-01, 2026-01-10] 北京时间：UTC 2025-12-31 16:00 ~ 2026-01-09 16:00，
    # UTC 2026-01-03 15:00（北京 01-02 23:00）与 16:00（北京 01-04 00:00）在窗内
    preset_payload(
        monkeypatch,
        forex_rows(
            [
                "2025-12-31T00:00:00.000Z",  # 北京 2025-12-31 08:00，窗前
                "2026-01-03T15:00:00.000Z",  # 北京 2026-01-02 23:00，窗内
                "2026-01-03T16:00:00.000Z",  # 北京 2026-01-04 00:00，窗内
                "2026-01-12T00:00:00.000Z",  # 北京 2026-01-12 08:00，窗外
            ]
        ),
    )
    adapter = _adapter()
    batch = asyncio.run(adapter.fetch_bars(_query(offset=1, limit=1)))

    assert batch.count == 1
    assert batch.offset == 1
    assert batch.bars[0].timestamp == datetime(2026, 1, 4, 0, tzinfo=CN_TIMEZONE)


def test_wrong_exchange_namespace_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """外汇必须使用 OTC 命名空间：exchange 段不符时映射层明确拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(instrument=Instrument.parse("GLOBAL-NYSE-EUR/USD"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_unsupported_asset_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tiingo 仅声明外汇/加密货币：期货请求在映射层拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(
        asset_type=AssetType.CONTRACT, instrument=Instrument.parse("CN-SHFE-RB2412")
    )
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


def test_cn_market_stock_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """仅声明 GLOBAL 市场：CN-SSE 标的在 operation 二级路由拒绝。"""
    install(monkeypatch)
    adapter = _adapter()
    query = _query(asset_type=AssetType.STOCK, instrument=Instrument.parse("CN-SSE-600000"))
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(adapter.fetch_bars(query))


@pytest.mark.parametrize("interval", ["P1W", "P1M", "PT2H", "P3D"])
def test_unsupported_intervals_rejected_by_operation_routing(
    interval: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游 resampleFreq 无周/月线，2h 等待探针实证：能力目录即不声明。"""
    install(monkeypatch)
    adapter = _adapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(_query(interval=Interval.parse(interval)))


def test_missing_api_key() -> None:
    adapter = _adapter(make_settings(tiingo_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "API key" in str(excinfo.value)


def test_missing_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "httpx", None)  # 模拟依赖未安装
    adapter = _adapter(make_settings(tiingo_api_key="k"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "httpx" in str(excinfo.value)


def test_rate_limited_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """免费档限频（50 请求/时、1000 请求/天）：429 明确映射 UPSTREAM_RATE_LIMITED。"""
    install(monkeypatch)
    preset_status(monkeypatch, 429, {"detail": "Rate limit exceeded"})
    adapter = _adapter()
    with pytest.raises(UpstreamRateLimitedError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_upstream_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """其余非 2xx（如无效凭据 401）：UPSTREAM_ERROR 携带 status_code。"""
    install(monkeypatch)
    preset_status(monkeypatch, 401, {"detail": "Invalid token"})
    adapter = _adapter()
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["status_code"] == 401


def test_non_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_status(monkeypatch, 200, RuntimeError("bad body"))  # 触发 .json() 解析失败
    adapter = _adapter()
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_non_array_payload_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游 bars 契约为 JSON 数组：非数组响应按契约违背拒绝。"""
    install(monkeypatch)
    preset_payload(monkeypatch, {"detail": "unexpected"})
    adapter = _adapter()
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


# ---------------------------------------------------------------------------
# 实时快照：急切校验（快照/快照流走 WS 订阅源，链路用例见
# test_tiingo_ws_runtime.py 与契约测试）
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
    adapter = _adapter(make_settings(tiingo_api_key=""))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert "API key" in str(excinfo.value)
