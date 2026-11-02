"""测试用 TickFlow SDK 桩：模拟延迟导入的 ``tickflow`` 模块。

只实现适配器使用的 ``TickFlow(api_key=...).klines.get(...)`` 路径，
记录构造与调用参数并返回文档核实的紧凑列式结构，无真实网络。
"""

from datetime import datetime, timezone
from typing import Any


def _to_millis(day: str) -> int:
    dt = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class StubKlines:
    def __init__(self, client: "StubTickFlow") -> None:
        self._client = client

    def get(self, **kwargs: Any) -> Any:
        self._client.calls.append(dict(kwargs))
        return self._client.payload


class StubTickFlow:
    """TickFlow 客户端桩；所有实例记录在 ``instances``。"""

    instances: list["StubTickFlow"] = []

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        self.api_key = api_key
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.payload: dict[str, list[Any]] = {
            "timestamp": [], "open": [], "high": [],
            "low": [], "close": [], "volume": [], "amount": [],
        }
        StubTickFlow.instances.append(self)
        self.klines = StubKlines(self)


def set_payload(client: StubTickFlow, payload: Any) -> None:
    client.payload = payload


def klines_payload(days: list[str]) -> dict[str, list[Any]]:
    """按官方文档构造紧凑列式响应：必填 6 字段 + prev_close。"""
    n = len(days)
    return {
        "timestamp": [_to_millis(day) for day in days],
        "open": [10.0] * n,
        "high": [10.3] * n,
        "low": [9.9] * n,
        "close": [10.2] * n,
        "volume": [100] * n,
        "amount": [1020.0] * n,
        "prev_close": [10.1] * n,
    }


def sized_klines_payload(n: int) -> dict[str, list[Any]]:
    """n 根 K 线的列式响应（时间戳任意），用于上游截断边界测试。"""
    base_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return {
        "timestamp": [base_ms + i * 60_000 for i in range(n)],
        "open": [1.0] * n,
        "high": [1.1] * n,
        "low": [0.9] * n,
        "close": [1.0] * n,
        "volume": [10] * n,
        "amount": [10.0] * n,
    }


def install(monkeypatch: Any) -> list[StubTickFlow]:
    """把桩模块注入 sys.modules，返回实例记录列表。"""
    import sys
    import types

    module = types.ModuleType("tickflow")
    module.TickFlow = StubTickFlow  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tickflow", module)
    StubTickFlow.instances.clear()
    return StubTickFlow.instances


def preset_payload(monkeypatch: Any, payload: Any) -> None:
    """让每个新建的桩客户端都返回指定响应（须在 install 之后调用）。"""
    original_init = StubTickFlow.__init__

    def init_with_payload(self: StubTickFlow, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.payload = payload

    monkeypatch.setattr(StubTickFlow, "__init__", init_with_payload)
