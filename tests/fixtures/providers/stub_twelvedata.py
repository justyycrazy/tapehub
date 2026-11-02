"""测试用 Twelve Data SDK 桩：模拟延迟导入的 ``twelvedata`` 模块。

只实现适配器使用的 ``TDClient(apikey=...).time_series(...).as_json()`` 路径，
记录调用参数并返回文档核实的 values 行（字符串数值 + datetime），无真实网络。
"""

from typing import Any


class StubTimeSeries:
    def __init__(self, client: "StubTDClient", kwargs: dict[str, Any]) -> None:
        self._client = client
        self._kwargs = kwargs

    def as_json(self) -> Any:
        self._client.calls.append(dict(self._kwargs))
        return self._client.payload


class StubTDClient:
    """TDClient 桩；所有实例记录在 ``instances``。"""

    instances: list["StubTDClient"] = []

    def __init__(self, apikey: str | None = None, **kwargs: Any) -> None:
        self.apikey = apikey
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.payload: Any = []
        StubTDClient.instances.append(self)

    def time_series(self, **kwargs: Any) -> StubTimeSeries:
        return StubTimeSeries(self, kwargs)


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
