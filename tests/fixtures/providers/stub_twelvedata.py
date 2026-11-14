"""测试用 Twelve Data SDK 桩：模拟延迟导入的 ``twelvedata`` 模块。

只实现适配器使用的两条路径（无真实网络）：
- ``TDClient(apikey=...).time_series(...).as_json()``：记录调用参数并返回
  文档核实的 values 行（字符串数值 + datetime）；
- ``TDClient(apikey=...).quote(...).as_json()``：记录调用参数并返回
  文档核实的 quote 响应对象（字符串数值 + Unix 秒时间戳）。
"""

from typing import Any


class StubTimeSeries:
    def __init__(self, client: "StubTDClient", kwargs: dict[str, Any]) -> None:
        self._client = client
        self._kwargs = kwargs

    def as_json(self) -> Any:
        self._client.calls.append(dict(self._kwargs))
        if self._client.error is not None:
            raise self._client.error
        return self._client.payload


class StubQuote:
    def __init__(self, client: "StubTDClient", kwargs: dict[str, Any]) -> None:
        self._client = client
        self._kwargs = kwargs

    def as_json(self) -> Any:
        self._client.quote_calls.append(dict(self._kwargs))
        if self._client.error is not None:
            raise self._client.error
        return self._client.quote_payload


class StubTDClient:
    """TDClient 桩；所有实例记录在 ``instances``。"""

    instances: list["StubTDClient"] = []

    def __init__(self, apikey: str | None = None, **kwargs: Any) -> None:
        self.apikey = apikey
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.payload: Any = []
        self.quote_calls: list[dict[str, Any]] = []
        self.quote_payload: Any = {}
        self.error: BaseException | None = None
        StubTDClient.instances.append(self)

    def time_series(self, **kwargs: Any) -> StubTimeSeries:
        return StubTimeSeries(self, kwargs)

    def quote(self, **kwargs: Any) -> StubQuote:
        return StubQuote(self, kwargs)


def install(monkeypatch: Any) -> type[StubTDClient]:
    """把桩模块注入 sys.modules，清空实例记录。"""
    import sys
    import types

    module = types.ModuleType("twelvedata")
    module.TDClient = StubTDClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "twelvedata", module)
    StubTDClient.instances.clear()
    return StubTDClient


def preset_values(monkeypatch: Any, values: list[dict[str, Any]]) -> None:
    """让每个新建的桩客户端都返回指定 values（须在 install 之后调用）。"""
    original_init = StubTDClient.__init__

    def init_with_payload(self: StubTDClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.payload = values

    monkeypatch.setattr(StubTDClient, "__init__", init_with_payload)


def preset_quote(monkeypatch: Any, payload: dict[str, Any]) -> None:
    """让每个新建的桩客户端的 quote 都返回指定 payload（须在 install 之后调用）。"""
    original_init = StubTDClient.__init__

    def init_with_quote(self: StubTDClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.quote_payload = payload

    monkeypatch.setattr(StubTDClient, "__init__", init_with_quote)


def preset_error(monkeypatch: Any, error: BaseException) -> None:
    """让每个新建的桩客户端的 as_json 抛出指定异常（须在 install 之后调用）。"""
    original_init = StubTDClient.__init__

    def init_with_error(self: StubTDClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.error = error

    monkeypatch.setattr(StubTDClient, "__init__", init_with_error)


def values_rows(datetimes: list[str]) -> list[dict[str, Any]]:
    """按官方文档构造 values 行：字段为字符串，含 volume（股票类才有）可选。"""
    return [
        {
            "datetime": day,
            "open": "1.0850",
            "high": "1.0860",
            "low": "1.0840",
            "close": "1.0855",
            "volume": "12345",
        }
        for day in datetimes
    ]


def forex_values_rows(datetimes: list[str]) -> list[dict[str, Any]]:
    """外汇 values 行：通常无 volume 字段。"""
    return [
        {
            "datetime": day,
            "open": "1.0850",
            "high": "1.0860",
            "low": "1.0840",
            "close": "1.0855",
        }
        for day in datetimes
    ]


def sized_values_rows(n: int, *, start_day: str = "2026-01-01") -> list[dict[str, Any]]:
    """n 根 K 线的 values 行（分钟步进），用于上游截断/覆盖边界测试。"""
    from datetime import datetime, timedelta, timezone

    base = datetime.fromisoformat(start_day).replace(tzinfo=timezone.utc)
    return [
        {
            "datetime": (base + i * timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": "1.0",
            "high": "1.1",
            "low": "0.9",
            "close": "1.0",
        }
        for i in range(n)
    ]


def forex_quote_payload() -> dict[str, Any]:
    """外汇 /quote 响应形（探针实测镜像）。

    数值字符串、时间戳为 Unix 秒；实测无 volume/currency/mic_code；
    datetime/timestamp 为 interval=1day 日 bar 开盘时刻，last_quote_at 为
    最后一根分钟 K 线时刻（快照新鲜度口径，实测≈墙钟实时）。
    """
    return {
        "symbol": "EUR/USD",
        "name": "Euro / US Dollar",
        "exchange": "Forex",  # 实测值（非 OTC）
        "datetime": "2026-01-05",
        "timestamp": 1767571200,  # 日 bar 开盘 2026-01-05T00:00:00Z
        "last_quote_at": 1767616440,  # 最后一根分钟 K 线 2026-01-05T12:34:00Z
        "open": "1.0850",
        "high": "1.0860",
        "low": "1.0840",
        "close": "1.0855",
        "previous_close": "1.0860",
        "change": "-0.0005",
        "percent_change": "-0.046",
        "is_market_open": True,
        "fifty_two_week": {  # 嵌套对象：白名单不透出，断言排除用
            "low": "1.11068",
            "high": "1.20656",
            "range": "1.110680 - 1.206560",
        },
    }


def crypto_quote_payload() -> dict[str, Any]:
    """加密货币 /quote 响应形（探针实测镜像 + 防御分支保留）。

    实测：exchange 为聚合源交易所 "Binance"，
    无 volume/currency，含 crypto 专属 rolling_*；本条保留 volume 字符串
    仅覆盖映射防御分支（部分品种文档口径可能返回），rolling_* 用于断言
    白名单排除。
    """
    return {
        "symbol": "BTC/USD",
        "name": "Bitcoin US Dollar",
        "exchange": "Binance",
        "datetime": "2026-01-05",
        "timestamp": 1767571200,
        "last_quote_at": 1767616440,
        "open": "42000.5",
        "high": "43500",
        "low": "41800.2",
        "close": "43120.75",
        "volume": "12345.6",  # 防御分支：实测 BTC/USD 未返回该字段
        "previous_close": "42800.1",
        "change": "320.65",
        "percent_change": "0.749",
        "is_market_open": True,
        "rolling_1d_change": "1.25",  # crypto 专属：白名单不透出
        "rolling_7d_change": "-3.4",
    }
