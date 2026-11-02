"""测试用 AlphaFeed SDK 桩：模拟延迟导入的 ``alphafeed`` 模块。

只实现适配器使用的 ``AlphaFeed(api_key=...).klines.get(...)`` 路径，
记录构造与调用参数并返回文档核实的紧凑列式结构（顶层 data 包装），
无真实网络。
"""

from datetime import datetime, timezone
from typing import Any


def _to_millis(day: str) -> int:
    dt = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class StubKlines:
    def __init__(self, client: "StubAlphaFeed") -> None:
        self._client = client

    def get(self, **kwargs: Any) -> Any:
        self._client.calls.append(dict(kwargs))
        return self._client.payload


class StubAlphaFeed:
    """AlphaFeed 客户端桩；所有实例记录在 ``instances``。"""

    instances: list["StubAlphaFeed"] = []

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        self.api_key = api_key
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.payload: Any = {
            "data": {
                "timestamp": [],
                "open": [],
                "high": [],
                "low": [],
                "close": [],
                "volume": [],
                "amount": [],
            }
        }
        StubAlphaFeed.instances.append(self)
        self.klines = StubKlines(self)


def install(monkeypatch: Any) -> list[StubAlphaFeed]:
    """把桩模块注入 sys.modules，返回实例记录列表。"""
    import sys
    import types

    module = types.ModuleType("alphafeed")
    module.AlphaFeed = StubAlphaFeed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alphafeed", module)
    StubAlphaFeed.instances.clear()
    return StubAlphaFeed.instances


def preset_payload(monkeypatch: Any, payload: Any) -> None:
    """让每个新建的桩客户端都返回指定响应（须在 install 之后调用）。"""
    original_init = StubAlphaFeed.__init__

    def init_with_payload(self: StubAlphaFeed, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.payload = payload

    monkeypatch.setattr(StubAlphaFeed, "__init__", init_with_payload)


def klines_payload(days: list[str]) -> dict[str, Any]:
    """按官方文档构造响应：{data: 紧凑列式}，含可选 prev_close。"""
    n = len(days)
    return {
        "data": {
            "timestamp": [_to_millis(day) for day in days],
            "open": [20.0] * n,
            "high": [20.3] * n,
            "low": [19.9] * n,
            "close": [20.2] * n,
            "volume": [200] * n,
            "amount": [4040.0] * n,
            "prev_close": [20.1] * n,
        }
    }


def sized_klines_payload(n: int) -> dict[str, Any]:
    """n 根 K 线的列式响应（时间戳任意），用于上游截断边界测试。"""
    base_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return {
        "data": {
            "timestamp": [base_ms + i * 60_000 for i in range(n)],
            "open": [2.0] * n,
            "high": [2.1] * n,
            "low": [1.9] * n,
            "close": [2.0] * n,
            "volume": [20] * n,
            "amount": [40.0] * n,
        }
    }
