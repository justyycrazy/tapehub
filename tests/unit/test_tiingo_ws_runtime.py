"""Tiingo WS Runtime 与适配器流式/快照链路单元测试（假 websockets 桩）。

覆盖（协议按官方文档 websockets 页实现；消息形态细节待探针实证）：

- Runtime：subscribed→quote 事件序列、订阅帧结构（authorization +
  eventData.thresholdLevel/tickers，按资产类型分流端点与级别）、缓存
  存在时监听器补发订阅确认、引用计数与 linger 真退订、data 元素按
  ticker 客户端兜底过滤、断连 upstream_disconnected→退避重连自动重订
  恢复、建连失败重试、致命错误（websockets 缺失）急切失败、停机通知、
  同端点多 Runtime 告警（异端点不告警）；
- 适配器：fetch_quote 冷订阅/缓存命中/首包超时 QUOTE_NOT_READY、
  stream_quote 首帧序列与关闭释放、coalesce 丢旧保新、按端点容量闸门
  UPSTREAM_SUBSCRIPTION_LIMIT；
- 映射：ws_price_to_quote 字段口径（TOP midPrice 回退、Trade lastPrice、
  bid/ask 防御映射、extensions 白名单、契约违背抛 UPSTREAM_ERROR）。
"""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Any

import pytest

import tapehub.providers.tiingo.runtime as tiingo_runtime
from tapehub.domain.errors import (
    ProviderNotImplementedError,
    QuoteNotReadyError,
    UpstreamError,
    UpstreamSubscriptionLimitError,
)
from tapehub.domain.models import CN_TIMEZONE, AssetType, DataVariant, Instrument, QuoteQuery
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tapehub.providers.tiingo.request_mapper import (
    WS_CRYPTO_ENDPOINT,
    WS_CRYPTO_THRESHOLD_LEVEL,
    WS_FX_ENDPOINT,
    WS_FX_THRESHOLD_LEVEL,
)
from tapehub.providers.tiingo.response_mapper import ws_element_to_snapshot, ws_price_to_quote
from tests.conftest import make_settings
from tests.fixtures.providers import fake_tiingo_ws as fake_ws
from tests.fixtures.providers.fake_tiingo_ws import FakeWebSocket, install

_SYMBOL = "eurusd"  # 上游 ticker 形态（小写连写）


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


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def test_runtime_subscribed_then_quote_events(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)

    async def scenario() -> list[tuple[str, str, dict[str, Any] | None]]:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        events: list[tuple[str, str, dict[str, Any] | None]] = []
        remove = runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append((k, s, p)))
        runtime.subscribe(_SYMBOL)
        for _ in range(60):
            if events and events[-1][0] == "quote":
                break
            await asyncio.sleep(0.01)
        ws = FakeWebSocket.instances[-1]
        ws.push_quote(_SYMBOL, fake_ws.top_element(_SYMBOL, bid=1.12, ask=1.13))
        await asyncio.sleep(0.05)
        remove()
        await runtime.shutdown()
        return events

    events = asyncio.run(scenario())
    kinds = [e[0] for e in events]
    assert kinds[0] == "subscribed"  # 首条匹配 data 元素 → 订阅确认
    assert "quote" in kinds
    first = next(e[2] for e in events if e[0] == "subscribed")
    assert first is not None and first["bidPrice"] == 1.085  # 假件首条（默认样本）
    last = next(e[2] for e in reversed(events) if e[0] == "quote")
    assert last is not None and last["bidPrice"] == 1.12


def test_subscribe_frame_structure_and_endpoint_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """订阅帧按官方文档构造：authorization + eventData（thresholdLevel +
    tickers）；端点与级别按资产类型分流（crypto=2、forex=7），token 不落 URL。"""
    install(monkeypatch)

    async def _wait(predicate: Any) -> None:
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met")

    async def scenario() -> tuple[FakeWebSocket, FakeWebSocket]:
        forex = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await forex.start()
        forex.subscribe("eurusd")
        crypto = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="crypto", subscription_limit=8
        )
        await crypto.start()
        crypto.subscribe("btcusd")
        # 等两端建连并发出订阅帧（start 只创建任务，连接异步完成；
        # 过早 shutdown 会取消尚未运行的连接任务）
        await _wait(lambda: len(FakeWebSocket.instances) >= 2)
        forex_ws = FakeWebSocket.instances[0]
        crypto_ws = FakeWebSocket.instances[1]
        await _wait(lambda: bool(forex_ws.subscribe_frames("eurusd")))
        await _wait(lambda: bool(crypto_ws.subscribe_frames("btcusd")))
        await forex.shutdown()
        await crypto.shutdown()
        return forex_ws, crypto_ws

    forex_ws, crypto_ws = asyncio.run(scenario())
    assert forex_ws.endpoint == WS_FX_ENDPOINT
    assert forex_ws.subscribe_frames("eurusd")[0] == {
        "eventName": "subscribe",
        "authorization": "k",
        "eventData": {"thresholdLevel": WS_FX_THRESHOLD_LEVEL, "tickers": ["eurusd"]},
    }
    assert crypto_ws.endpoint == WS_CRYPTO_ENDPOINT
    assert crypto_ws.subscribe_frames("btcusd")[0]["eventData"] == {
        "thresholdLevel": WS_CRYPTO_THRESHOLD_LEVEL,
        "tickers": ["btcusd"],
    }


def test_listener_replay_when_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """监听器注册时快照已存在 → 立即补发 subscribed（先查快照再开流不静默）。"""
    install(monkeypatch)

    async def scenario() -> tuple[list[str], dict[str, Any] | None]:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        runtime.subscribe(_SYMBOL)
        for _ in range(60):
            if runtime.latest(_SYMBOL) is not None:
                break
            await asyncio.sleep(0.01)
        replayed: list[tuple[str, dict[str, Any] | None]] = []
        remove = runtime.add_stream_listener(_SYMBOL, lambda k, s, p: replayed.append((k, p)))
        remove()  # 快照补发是同步的，注册即发生；先注销再停机避免混入断流事件
        await runtime.shutdown()
        return [k for k, _ in replayed], replayed[0][1] if replayed else None

    kinds, snapshot = asyncio.run(scenario())
    assert kinds == ["subscribed"]
    assert snapshot is not None and snapshot["ticker"] == _SYMBOL


def test_data_filtered_by_ticker(monkeypatch: pytest.MonkeyPatch) -> None:
    """data 元素按 ticker 客户端兜底过滤：服务端 tickers 过滤未实证，
    同帧中的非订阅 ticker 直接忽略，不影响本订阅语义。"""
    install(monkeypatch)

    async def scenario() -> tuple[list[str], dict[str, Any] | None]:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        runtime.subscribe(_SYMBOL)
        events: list[tuple[str, str]] = []
        remove = runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append((k, s)))
        for _ in range(60):
            if runtime.latest(_SYMBOL) is not None:
                break
            await asyncio.sleep(0.01)
        ws = FakeWebSocket.instances[-1]
        # 同一 data 帧：外币对元素 + 本订阅元素（服务端过滤失效时的形态）
        ws.push_quote(
            _SYMBOL,
            fake_ws.top_element("gbpusd", bid=2.0, ask=2.01),
            fake_ws.top_element(_SYMBOL, bid=1.10, ask=1.11),
        )
        await asyncio.sleep(0.05)
        remove()
        await runtime.shutdown()
        return [k for k, _ in events], runtime.latest(_SYMBOL)

    kinds, snapshot = asyncio.run(scenario())
    assert kinds == ["subscribed", "quote"]  # 外币对元素未触发任何事件
    assert snapshot is not None and snapshot["bidPrice"] == 1.10  # 只保留本订阅最新值


def test_refcount_and_real_unsubscribe_after_linger(monkeypatch: pytest.MonkeyPatch) -> None:
    """引用计数：双重订阅一次释放不退订；归零后经 linger 发上游真退订。"""
    install(monkeypatch)
    monkeypatch.setattr(tiingo_runtime, "_SNAPSHOT_LINGER_SECONDS", 0.05)

    async def scenario() -> FakeWebSocket:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        runtime.subscribe(_SYMBOL)
        runtime.subscribe(_SYMBOL)
        for _ in range(60):
            if runtime.latest(_SYMBOL) is not None:
                break
            await asyncio.sleep(0.01)
        runtime.unsubscribe(_SYMBOL)  # 仍剩 1 个引用
        await asyncio.sleep(0.1)
        assert runtime.latest(_SYMBOL) is not None  # 未退订，缓存仍在
        runtime.unsubscribe(_SYMBOL)  # 归零 → linger → 真退订
        for _ in range(60):
            if runtime.latest(_SYMBOL) is None:
                break
            await asyncio.sleep(0.01)
        assert runtime.active_symbols() == []
        ws = FakeWebSocket.instances[-1]
        assert len(ws.unsubscribe_frames(_SYMBOL)) == 1  # 只在归零 linger 后退订一次
        await runtime.shutdown()
        return ws

    asyncio.run(scenario())


def test_disconnect_reconnect_resubscribes(monkeypatch: pytest.MonkeyPatch) -> None:
    """断连广播 upstream_disconnected → 退避重连自动重订 → subscribed 恢复。"""
    install(monkeypatch)
    monkeypatch.setattr(tiingo_runtime, "_RECONNECT_BACKOFF_SECONDS", 0.02)

    async def scenario() -> tuple[list[str], int]:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        events: list[str] = []
        runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append(k))
        runtime.subscribe(_SYMBOL)
        for _ in range(60):
            if "quote" in events:
                break
            await asyncio.sleep(0.01)
        first_ws = FakeWebSocket.instances[-1]
        first_ws.drop_connection()  # 注入断连
        for _ in range(200):
            if events.count("subscribed") >= 2:
                break
            await asyncio.sleep(0.01)
        await runtime.shutdown()
        return events, len(FakeWebSocket.instances)

    events, connections = asyncio.run(scenario())
    assert "upstream_disconnected" in events
    assert events.count("subscribed") >= 2  # 重订恢复（绝不静默断流）
    assert connections >= 2  # 确实重建了连接


def test_rejected_ticker_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游不推数据（拒收形态未实证）：等 first-packet 由适配器超时兜底，
    Runtime 不编造订阅确认。"""
    install(monkeypatch)
    FakeWebSocket.reject_symbols = {_SYMBOL}

    async def scenario() -> list[str]:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        events: list[str] = []
        remove = runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append(k))
        runtime.subscribe(_SYMBOL)
        await asyncio.sleep(0.05)
        captured = list(events)  # 停机会广播断流事件，先取快照
        remove()
        await runtime.shutdown()
        return captured

    assert asyncio.run(scenario()) == []


def test_connect_failure_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    FakeWebSocket.connect_failures = 2
    monkeypatch.setattr(tiingo_runtime, "_RECONNECT_BACKOFF_SECONDS", 0.02)

    async def scenario() -> dict[str, Any] | None:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        runtime.subscribe(_SYMBOL)
        for _ in range(200):
            if runtime.latest(_SYMBOL) is not None:
                break
            await asyncio.sleep(0.01)
        snapshot = runtime.latest(_SYMBOL)
        await runtime.shutdown()
        return snapshot

    assert asyncio.run(scenario()) is not None  # 建连失败重试后仍成功


def test_shutdown_notifies_listeners(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)

    async def scenario() -> list[str]:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()
        events: list[str] = []
        runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append(k))
        await runtime.shutdown()
        return events

    assert asyncio.run(scenario()) == ["upstream_disconnected"]


def test_runtime_missing_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "websockets", None)  # 模拟库未安装

    async def scenario() -> None:
        runtime = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await runtime.start()

    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(scenario())
    assert "websockets" in str(excinfo.value)


def test_second_live_runtime_same_endpoint_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """tripwire 按端点判定：同端点第二个存活 Runtime 启动即告警；
    crypto/forex 异端点并存不告警（各自单连接互不干扰）。"""
    install(monkeypatch)
    caplog.set_level(logging.WARNING, logger="tapehub.providers.tiingo.runtime")

    async def scenario() -> None:
        forex = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await forex.start()
        await forex.start()  # 幂等重启（同一实例）：不应告警
        crypto = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="crypto", subscription_limit=8
        )
        await crypto.start()  # 异端点：不应告警
        second_forex = tiingo_runtime.TiingoWsRuntime(
            api_key="k", asset_type="forex", subscription_limit=8
        )
        await second_forex.start()  # 同端点第二个存活实例：应告警
        await second_forex.shutdown()
        await crypto.shutdown()
        await forex.shutdown()

    asyncio.run(scenario())
    warnings = [
        record for record in caplog.records if "other live runtime" in record.message
    ]
    assert len(warnings) == 1  # 恰好只在 second_forex.start() 时告警一次


# ---------------------------------------------------------------------------
# 适配器：M2 快照（WS 订阅缓存）
# ---------------------------------------------------------------------------


def test_fetch_quote_cold_subscription_and_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷订阅等首包 → 标准 Quote；linger 窗口内再次请求读缓存（免重订）。"""
    install(monkeypatch)

    async def scenario() -> tuple[Any, Any, int]:
        adapter = _adapter(make_settings(tiingo_api_key="test-key"))
        quote1 = await adapter.fetch_quote(_quote_query())
        quote2 = await adapter.fetch_quote(_quote_query())
        ws = FakeWebSocket.instances[-1]
        runtime = adapter._runtimes["forex"]
        await runtime.shutdown()
        return quote1, quote2, len(ws.subscribe_frames("eurusd"))

    quote1, quote2, subscribes = asyncio.run(scenario())
    assert quote1.provider == "tiingo"
    # TOP 元素无 lastPrice → last_price 回退 midPrice（文档：bid/ask 均非空时
    # mid=(bid+ask)/2；假件样本 bid 1.085 / ask 1.086 → mid 1.0855）
    assert quote1.last_price == 1.0855
    # date ISO UTC → 统一北京时间
    assert quote1.timestamp == datetime(2026, 1, 5, 8, tzinfo=CN_TIMEZONE)
    assert quote1.volume is None and quote1.amount is None  # WS 形态未实证恒 null
    # 防御映射：bid/ask 第一档（文档核实字段，五档其余恒 null）
    assert quote1.bid_price1 == 1.085
    assert quote1.ask_price1 == 1.086
    assert quote1.bid_volume1 == 1.0
    assert quote1.ask_volume1 == 1.0
    for i in range(2, 6):
        assert getattr(quote1, f"bid_price{i}") is None
        assert getattr(quote1, f"ask_price{i}") is None
    assert quote1.extensions is None  # 样本无 exchange（forex top 文档无该字段）
    assert quote2.last_price == 1.0855
    assert subscribes == 1  # 第二次请求命中缓存，未重复订阅


def test_fetch_quote_crypto_trade_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """加密 Trade 元素：lastPrice 直接取值、exchange 进 extensions 白名单。"""
    install(monkeypatch)
    FakeWebSocket.initial_elements = {
        "btcusd": fake_ws.trade_element("btcusd", 43120.75, exchange="Binance")
    }

    async def scenario() -> Any:
        adapter = _adapter(make_settings(tiingo_api_key="k"))
        quote = await adapter.fetch_quote(
            _quote_query(
                asset_type=AssetType.CRYPTO,
                instrument=Instrument.parse("GLOBAL-CRYPTO-BTC/USD"),
            )
        )
        runtime = adapter._runtimes["crypto"]
        await runtime.shutdown()
        return quote

    quote = asyncio.run(scenario())
    assert quote.last_price == 43120.75
    assert quote.extensions == {"exchange": "Binance"}


def test_fetch_quote_timeout_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷订阅首包超时 → QUOTE_NOT_READY（不编造部分字段快照）；引用正常释放。"""
    install(monkeypatch)
    FakeWebSocket.first_price_delay = 0.5

    async def scenario() -> None:
        adapter = _adapter(
            make_settings(tiingo_api_key="k", tiingo_first_snapshot_timeout=0.05)
        )
        try:
            with pytest.raises(QuoteNotReadyError):
                await adapter.fetch_quote(_quote_query())
        finally:
            await adapter._runtimes["forex"].shutdown()

    asyncio.run(scenario())


def test_fetch_quote_missing_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "websockets", None)
    adapter = _adapter(make_settings(tiingo_api_key="k"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert "websockets" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 适配器：M3 流式
# ---------------------------------------------------------------------------


def test_adapter_stream_quote_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """流事件序列：subscribed（状态帧）→ 首条 quote → 后续 quote；关闭释放引用。"""
    install(monkeypatch)
    monkeypatch.setattr(tiingo_runtime, "_SNAPSHOT_LINGER_SECONDS", 0.05)

    async def scenario() -> tuple[list[str], list[float], FakeWebSocket]:
        adapter = _adapter(make_settings(tiingo_api_key="k"))
        stream = adapter.stream_quote(_quote_query())
        kinds: list[str] = []
        prices: list[float] = []
        try:
            event = await asyncio.wait_for(anext(stream), timeout=2)
            assert event.status is not None
            kinds.append(event.status.value)
            ws = FakeWebSocket.instances[-1]  # 连接已建立（首次迭代时懒启动）
            event = await asyncio.wait_for(anext(stream), timeout=2)
            assert event.quote is not None
            kinds.append("quote")
            prices.append(event.quote.last_price)
            ws.push_quote(_SYMBOL, fake_ws.top_element(_SYMBOL, bid=1.13, ask=1.14))
            event = await asyncio.wait_for(anext(stream), timeout=2)
            assert event.quote is not None
            kinds.append("quote")
            prices.append(event.quote.last_price)
        finally:
            await stream.aclose()
            await asyncio.sleep(0.1)  # linger 真退订
            await adapter._runtimes["forex"].shutdown()
        return kinds, prices, ws

    kinds, prices, ws = asyncio.run(scenario())
    assert kinds == ["subscribed", "quote", "quote"]
    assert prices == [1.0855, 1.135]  # mid=(bid+ask)/2
    # 关闭即释放：linger 后上游收到 unsubscribe
    assert len(ws.unsubscribe_frames("eurusd")) == 1


def test_adapter_stream_quote_coalesce_keeps_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    """慢客户端：缓冲满丢旧保新只约束 quote 帧（最新价比完整有价值）。"""
    install(monkeypatch)
    FakeWebSocket.auto_price_seconds = 0.01

    async def scenario() -> float:
        adapter = _adapter(
            make_settings(tiingo_api_key="k", stream_queue_size=1)
        )
        stream = adapter.stream_quote(_quote_query())
        try:
            await asyncio.wait_for(anext(stream), timeout=2)  # subscribed
            await asyncio.wait_for(anext(stream), timeout=2)  # 首条 quote
            await asyncio.sleep(0.3)  # 生产者持续推进，缓冲挤掉旧 quote
            event = await asyncio.wait_for(anext(stream), timeout=2)
            assert event.quote is not None
            return event.quote.last_price
        finally:
            await stream.aclose()
            await adapter._runtimes["forex"].shutdown()

    assert asyncio.run(scenario()) > 1.0


def test_adapter_capacity_gate_per_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """容量闸门按端点生效：forex 端点占满后新 forex symbol 急切 429，
    crypto 端点不受影响（独立 Runtime）。"""
    install(monkeypatch)
    settings = make_settings(tiingo_api_key="k", tiingo_subscription_limit=1)

    async def scenario() -> None:
        adapter = _adapter(settings)
        stream = adapter.stream_quote(_quote_query())
        await asyncio.wait_for(anext(stream), timeout=2)  # forex 端点占满唯一额度
        # 同端点新 symbol：容量闸门在 stream_quote 调用时急切拒绝（SSE 头之前）
        with pytest.raises(UpstreamSubscriptionLimitError) as excinfo:
            adapter.stream_quote(
                _quote_query(instrument=Instrument.parse("GLOBAL-OTC-EUR/GBP"))
            )
        assert excinfo.value.details["limit"] == 1
        # 异端点（crypto）不受 forex 额度影响
        crypto_stream = adapter.stream_quote(
            _quote_query(
                asset_type=AssetType.CRYPTO,
                instrument=Instrument.parse("GLOBAL-CRYPTO-BTC/USD"),
            )
        )
        event = await asyncio.wait_for(anext(crypto_stream), timeout=2)
        assert event.status is not None
        await crypto_stream.aclose()
        await stream.aclose()
        await adapter._runtimes["forex"].shutdown()
        await adapter._runtimes["crypto"].shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ws_element_to_snapshot 标准化 + ws_price_to_quote 映射
# ---------------------------------------------------------------------------


def test_ws_element_to_snapshot_shapes() -> None:
    """data 元素位置解析（WS 探针实证的三种形状）：fx Q 8 元素
    （无 exchange）、crypto Q 9 元素 / T 6 元素（带 exchange）；未知形状
    返回空 dict（runtime 按 ticker 缺失丢弃）；对象形态透传。"""
    fx = ws_element_to_snapshot(
        fake_ws.top_element("eurusd", bid=1.085, ask=1.086)
    )
    assert fx["ticker"] == "eurusd"
    assert fx["bidPrice"] == 1.085 and fx["askPrice"] == 1.086
    assert fx["midPrice"] == 1.0855  # mid=(bid+ask)/2
    assert fx["bidSize"] == 1.0 and fx["askSize"] == 1.0
    assert "exchange" not in fx  # fx 无 exchange（8 元素）

    crypto_q = ws_element_to_snapshot(
        fake_ws.top_element("btcusd", bid=69207.0, ask=69207.01, exchange="gdax")
    )
    assert crypto_q["exchange"] == "gdax"  # crypto 9 元素带 exchange
    assert crypto_q["midPrice"] == 69207.005

    trade = ws_element_to_snapshot(
        fake_ws.trade_element("btcusd", 69207.19, exchange="bitstamp")
    )
    assert trade["lastPrice"] == 69207.19 and trade["lastSize"] == 0.5
    assert trade["exchange"] == "bitstamp"

    assert ws_element_to_snapshot(["Q", "eurusd"]) == {}  # 形状不足
    assert ws_element_to_snapshot({"ticker": "eurusd"}) == {"ticker": "eurusd"}  # 对象透传


def test_ws_price_to_quote_top_mid_fallback() -> None:
    """TOP 元素：无 lastPrice 时 last_price 回退 midPrice；bid/ask 防御映射。"""
    snapshot = ws_element_to_snapshot(fake_ws.top_element("eurusd", bid=1.085, ask=1.086))
    quote = ws_price_to_quote(
        snapshot, provider="tiingo", received_at=datetime.now(CN_TIMEZONE)
    )
    assert quote.last_price == 1.0855  # mid=(bid+ask)/2
    assert quote.bid_price1 == 1.085
    assert quote.bid_volume1 == 1.0
    assert quote.ask_price1 == 1.086
    assert quote.ask_volume1 == 1.0
    assert quote.volume is None and quote.amount is None
    assert quote.extensions is None


def test_ws_price_to_quote_trade_last_price() -> None:
    snapshot = ws_element_to_snapshot(
        fake_ws.trade_element("btcusd", 43120.75, exchange="binance")
    )
    quote = ws_price_to_quote(
        snapshot, provider="tiingo", received_at=datetime.now(CN_TIMEZONE)
    )
    assert quote.last_price == 43120.75
    assert quote.extensions == {"exchange": "binance"}


def test_ws_price_to_quote_contract_violations() -> None:
    now = datetime.now(CN_TIMEZONE)
    with pytest.raises(UpstreamError):  # 非 JSON 对象
        ws_price_to_quote(["unexpected"], provider="tiingo", received_at=now)
    snapshot = ws_element_to_snapshot(fake_ws.trade_element("eurusd", 1.1))
    snapshot.pop("date")
    with pytest.raises(UpstreamError) as excinfo:  # 缺时间：不伪造
        ws_price_to_quote(snapshot, provider="tiingo", received_at=now)
    assert excinfo.value.details["field"] == "date"
    with pytest.raises(UpstreamError):  # lastPrice/midPrice 均缺失：不伪造
        ws_price_to_quote(
            {"date": fake_ws.INITIAL_DATE}, provider="tiingo", received_at=now
        )
