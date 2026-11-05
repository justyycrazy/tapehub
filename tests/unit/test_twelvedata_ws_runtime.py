"""Twelve Data WS Runtime 与适配器流式/快照链路单元测试（假 websockets 桩）。

覆盖（按 2026-11-05 探针实证语义，报告见 probes/results/twelvedata/）：

- Runtime：subscribed→quote 事件序列、缓存存在时监听器补发订阅确认、
  引用计数与 linger 真退订（上游 unsubscribe 释放 credit）、断连
  upstream_disconnected→退避重连自动重订恢复、上游拒订以断流事件收尾、
  建连失败重试、致命错误（websockets 缺失）急切失败、停机通知；
- 适配器：fetch_quote 冷订阅/缓存命中/首包超时 QUOTE_NOT_READY、
  stream_quote 首帧序列与关闭释放、coalesce 丢旧保新、容量闸门
  UPSTREAM_SUBSCRIPTION_LIMIT；
- 映射：ws_price_to_quote 字段口径（timestamp 分钟桶、extensions 白名单、
  契约违背抛 UPSTREAM_ERROR）。
"""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Any

import pytest

import tapehub.providers.twelvedata.runtime as td_runtime
from tapehub.domain.errors import (
    ProviderNotImplementedError,
    QuoteNotReadyError,
    UpstreamError,
    UpstreamSubscriptionLimitError,
)
from tapehub.domain.models import CN_TIMEZONE, AssetType, DataVariant, Instrument, QuoteQuery
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tapehub.providers.twelvedata.response_mapper import ws_price_to_quote
from tapehub.settings import Settings
from tests.conftest import make_settings
from tests.fixtures.providers import fake_websockets as fake_ws
from tests.fixtures.providers.fake_websockets import FakeWebSocket, install

_SYMBOL = "EUR/USD"


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


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def test_runtime_subscribed_then_quote_events(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)

    async def scenario() -> list[tuple[str, str, dict[str, Any] | None]]:
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
        await runtime.start()
        events: list[tuple[str, str, dict[str, Any] | None]] = []
        remove = runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append((k, s, p)))
        runtime.subscribe(_SYMBOL)
        for _ in range(60):
            if events and events[-1][0] == "quote":
                break
            await asyncio.sleep(0.01)
        ws = FakeWebSocket.instances[-1]
        ws.push_price(_SYMBOL, 1.13001)
        await asyncio.sleep(0.05)
        remove()
        await runtime.shutdown()
        return events

    events = asyncio.run(scenario())
    kinds = [e[0] for e in events]
    assert kinds[0] == "subscribed"  # 首包 → 订阅确认（与 tqsdk 首包口径一致）
    assert "quote" in kinds
    first_price = next(e[2] for e in events if e[0] == "subscribed")
    assert first_price is not None and first_price["price"] == fake_ws.INITIAL_PRICE
    last_quote = next(e[2] for e in reversed(events) if e[0] == "quote")
    assert last_quote is not None and last_quote["price"] == 1.13001


def test_listener_replay_when_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """监听器注册时快照已存在 → 立即补发 subscribed（先查快照再开流不静默）。"""
    install(monkeypatch)

    async def scenario() -> tuple[list[str], dict[str, Any] | None]:
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
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
    assert snapshot is not None and snapshot["symbol"] == _SYMBOL


def test_refcount_and_real_unsubscribe_after_linger(monkeypatch: pytest.MonkeyPatch) -> None:
    """引用计数：双重订阅一次释放不退订；归零后经 linger 发上游真退订。"""
    install(monkeypatch)
    monkeypatch.setattr(td_runtime, "_SNAPSHOT_LINGER_SECONDS", 0.05)

    async def scenario() -> FakeWebSocket:
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
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
        unsubscribes = [
            m for m in ws.sent if m.get("action") == "unsubscribe"
        ]
        assert len(unsubscribes) == 1  # 只在归零 linger 后退订一次
        await runtime.shutdown()
        return ws

    asyncio.run(scenario())


def test_disconnect_reconnect_resubscribes(monkeypatch: pytest.MonkeyPatch) -> None:
    """断连广播 upstream_disconnected → 退避重连自动重订 → subscribed 恢复。"""
    install(monkeypatch)
    monkeypatch.setattr(td_runtime, "_RECONNECT_BACKOFF_SECONDS", 0.02)

    async def scenario() -> tuple[list[str], int]:
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
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


def test_rejected_subscription_ends_with_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游拒订（subscribe-status.fails）→ 该 symbol 以断流事件收尾。"""
    install(monkeypatch)
    FakeWebSocket.reject_symbols = {_SYMBOL}

    async def scenario() -> list[str]:
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
        await runtime.start()
        events: list[str] = []
        remove = runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append(k))
        runtime.subscribe(_SYMBOL)
        for _ in range(60):
            if events:
                break
            await asyncio.sleep(0.01)
        captured = list(events)  # 停机会广播断流事件，先取快照
        remove()
        await runtime.shutdown()
        return captured

    assert asyncio.run(scenario()) == ["upstream_disconnected"]


def test_connect_failure_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    FakeWebSocket.connect_failures = 2
    monkeypatch.setattr(td_runtime, "_RECONNECT_BACKOFF_SECONDS", 0.02)

    async def scenario() -> dict[str, Any] | None:
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
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
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
        await runtime.start()
        events: list[str] = []
        runtime.add_stream_listener(_SYMBOL, lambda k, s, p: events.append(k))
        await runtime.shutdown()
        return events

    assert asyncio.run(scenario()) == ["upstream_disconnected"]


def test_runtime_missing_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "websockets", None)  # 模拟库未安装

    async def scenario() -> None:
        runtime = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
        await runtime.start()

    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(scenario())
    assert "websockets" in str(excinfo.value)


def test_second_live_runtime_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """tripwire：同进程第二个存活 Runtime 启动即告警（上游 1 连接互踢）；
    已停机/已消亡的实例不计入，同一实例幂等重启不误报。"""
    install(monkeypatch)
    caplog.set_level(logging.WARNING, logger="tapehub.providers.twelvedata.runtime")

    async def scenario() -> None:
        first = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
        await first.start()  # 首个实例：不应告警
        await first.start()  # 幂等重启（同一实例）：不应告警
        second = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
        await second.start()  # 第二个存活实例：应告警
        await second.shutdown()
        await first.shutdown()  # 全部停机后不再有存活实例
        third = td_runtime.TwelvedataWsRuntime(api_key="k", subscription_limit=8)
        await third.start()  # 已停实例不计入：不应再告警
        await third.shutdown()

    asyncio.run(scenario())
    warnings = [
        record for record in caplog.records if "other live runtime" in record.message
    ]
    assert len(warnings) == 1  # 恰好只在 second.start() 时告警一次
    assert "single WS connection" in warnings[0].message


# ---------------------------------------------------------------------------
# 适配器：M2 快照（WS 订阅缓存）
# ---------------------------------------------------------------------------


def test_fetch_quote_cold_subscription_and_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷订阅等首包 → 标准 Quote；linger 窗口内再次请求读缓存（免重订）。"""
    install(monkeypatch)

    async def scenario() -> tuple[Any, Any, int]:
        adapter = _adapter(make_settings(twelvedata_api_key="test-key"))
        quote1 = await adapter.fetch_quote(_quote_query())
        quote2 = await adapter.fetch_quote(_quote_query())
        ws = FakeWebSocket.instances[-1]
        runtime = adapter._runtime
        await runtime.shutdown()
        return quote1, quote2, len([m for m in ws.sent if m.get("action") == "subscribe"])

    quote1, quote2, subscribes = asyncio.run(scenario())
    assert quote1.provider == "twelvedata"
    assert quote1.last_price == fake_ws.INITIAL_PRICE
    assert quote1.timestamp == datetime.fromtimestamp(
        fake_ws.INITIAL_TIMESTAMP, tz=CN_TIMEZONE
    )
    assert quote1.volume is None and quote1.amount is None  # WS 无源字段不用 0 冒充
    for i in range(1, 6):  # 上游无盘口：五档恒 null
        assert getattr(quote1, f"bid_price{i}") is None
        assert getattr(quote1, f"ask_price{i}") is None
    assert quote1.extensions == {
        "currency_base": "Euro",
        "currency_quote": "US Dollar",
        "type": "Physical Currency",  # EUR/USD price 事件无 exchange（探针实证）
    }
    assert quote2.last_price == fake_ws.INITIAL_PRICE
    assert subscribes == 1  # 第二次请求命中缓存，未重复订阅


def test_fetch_quote_timeout_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷订阅首包超时 → QUOTE_NOT_READY（不编造部分字段快照）；引用正常释放。"""
    install(monkeypatch)
    FakeWebSocket.first_price_delay = 0.5

    async def scenario() -> Any:
        adapter = _adapter(
            make_settings(twelvedata_api_key="k", twelvedata_first_snapshot_timeout=0.05)
        )
        try:
            with pytest.raises(QuoteNotReadyError):
                await adapter.fetch_quote(_quote_query())
        finally:
            await adapter._runtime.shutdown()

    asyncio.run(scenario())


def test_fetch_quote_missing_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "websockets", None)
    adapter = _adapter(make_settings(twelvedata_api_key="k"))
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert "websockets" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 适配器：M3 流式
# ---------------------------------------------------------------------------


def test_adapter_stream_quote_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """流事件序列：subscribed（状态帧）→ 首条 quote → 后续 quote；关闭释放引用。"""
    install(monkeypatch)
    monkeypatch.setattr(td_runtime, "_SNAPSHOT_LINGER_SECONDS", 0.05)

    async def scenario() -> tuple[list[str], list[float], FakeWebSocket]:
        adapter = _adapter(make_settings(twelvedata_api_key="k"))
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
            ws.push_price(_SYMBOL, 1.13)
            event = await asyncio.wait_for(anext(stream), timeout=2)
            assert event.quote is not None
            kinds.append("quote")
            prices.append(event.quote.last_price)
        finally:
            await stream.aclose()
            await asyncio.sleep(0.1)  # linger 真退订
            await adapter._runtime.shutdown()
        return kinds, prices, ws

    kinds, prices, ws = asyncio.run(scenario())
    assert kinds == ["subscribed", "quote", "quote"]
    assert prices == [fake_ws.INITIAL_PRICE, 1.13]
    # 关闭即释放：linger 后上游收到 unsubscribe（真退订，释放 credit）
    assert any(m.get("action") == "unsubscribe" for m in ws.sent)


def test_adapter_stream_quote_coalesce_keeps_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    """慢客户端：缓冲满丢旧保新只约束 quote 帧（最新价比完整有价值）。"""
    install(monkeypatch)
    FakeWebSocket.auto_price_seconds = 0.01

    async def scenario() -> float:
        adapter = _adapter(
            make_settings(twelvedata_api_key="k", stream_queue_size=1)
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
            await adapter._runtime.shutdown()

    assert asyncio.run(scenario()) > fake_ws.INITIAL_PRICE


def test_adapter_capacity_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """并发订阅 symbol 数达上限：新 symbol 的流请求急切 429（响应头之前）。"""
    install(monkeypatch)
    settings = make_settings(twelvedata_api_key="k", twelvedata_subscription_limit=1)

    async def scenario() -> None:
        adapter = _adapter(settings)
        stream = adapter.stream_quote(_quote_query())
        await asyncio.wait_for(anext(stream), timeout=2)  # 首个流占用唯一额度
        # 容量闸门在 stream_quote 调用时急切拒绝（SSE 响应头之前 → JSON 429）
        with pytest.raises(UpstreamSubscriptionLimitError) as excinfo:
            adapter.stream_quote(
                _quote_query(
                    asset_type=AssetType.CRYPTO,
                    instrument=Instrument.parse("GLOBAL-CRYPTO-BTC/USD"),
                )
            )
        assert excinfo.value.details["limit"] == 1
        await stream.aclose()
        await adapter._runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ws_price_to_quote 映射
# ---------------------------------------------------------------------------


def test_ws_price_to_quote_forex_payload() -> None:
    payload = fake_ws.price_event(_SYMBOL, 1.12183)
    quote = ws_price_to_quote(
        payload, provider="twelvedata", received_at=datetime.now(CN_TIMEZONE)
    )
    assert quote.last_price == 1.12183
    assert quote.timestamp == datetime.fromtimestamp(
        fake_ws.INITIAL_TIMESTAMP, tz=CN_TIMEZONE
    )
    assert quote.volume is None and quote.amount is None
    assert quote.extensions == {
        "currency_base": "Euro",
        "currency_quote": "US Dollar",
        "type": "Physical Currency",
    }


def test_ws_price_to_quote_crypto_exchange_field() -> None:
    payload = fake_ws.price_event("BTC/USD", 79838.73)
    payload["exchange"] = "Binance"  # 加密货币带 exchange（探针实证）
    payload.pop("type", None)
    quote = ws_price_to_quote(
        payload, provider="twelvedata", received_at=datetime.now(CN_TIMEZONE)
    )
    assert quote.extensions == {
        "currency_base": "Euro",
        "currency_quote": "US Dollar",
        "exchange": "Binance",
    }


def test_ws_price_to_quote_contract_violations() -> None:
    now = datetime.now(CN_TIMEZONE)
    with pytest.raises(UpstreamError):  # 非 JSON 对象
        ws_price_to_quote(["unexpected"], provider="twelvedata", received_at=now)
    payload = fake_ws.price_event(_SYMBOL, 1.1)
    payload.pop("timestamp")
    with pytest.raises(UpstreamError):  # 缺时间戳：不伪造时间
        ws_price_to_quote(payload, provider="twelvedata", received_at=now)
    payload = fake_ws.price_event(_SYMBOL, 1.1)
    payload["price"] = None
    with pytest.raises(UpstreamError):  # 缺价格：不伪造
        ws_price_to_quote(payload, provider="twelvedata", received_at=now)
