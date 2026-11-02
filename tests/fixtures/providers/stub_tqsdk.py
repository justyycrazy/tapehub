"""测试用 TqSdk SDK 桩：模拟延迟导入的 ``tqsdk`` 模块。

只实现适配器使用的 ``TqApi(auth=TqAuth(user, pwd)).get_kline_serial(...)`` 路径，
记录构造与调用参数并返回文档核实的列式行（dict 列表 + to_dict 桩 DataFrame），
无真实网络。
"""

from datetime import datetime, timezone
from typing import Any


def _to_ns(day: str) -> int:
    dt = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


class StubDataFrame:
    """最小 DataFrame 桩：只暴露适配器使用的 ``to_dict("records")``。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str | None = None) -> list[dict[str, Any]]:
        return list(self._rows)


class StubTqAuth:
    instances: list["StubTqAuth"] = []

    def __init__(self, user: str | None = None, password: str | None = None) -> None:
        self.user = user
        self.password = password
        StubTqAuth.instances.append(self)


class StubTqApi:
    """TqApi 客户端桩；所有实例记录在 ``instances``。"""

    instances: list["StubTqApi"] = []

    def __init__(self, auth: Any = None, **kwargs: Any) -> None:
        self.auth = auth
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.closed = False
        StubTqApi.instances.append(self)

    def get_kline_serial(self, **kwargs: Any) -> StubDataFrame:
        self.calls.append(dict(kwargs))
        return StubDataFrame(self.rows)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "StubTqApi":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def install(monkeypatch: Any) -> StubTqApi:
    """把桩模块注入 sys.modules，清空实例记录。"""
    import sys
    import types

    module = types.ModuleType("tqsdk")
    module.TqApi = StubTqApi  # type: ignore[attr-defined]
    module.TqAuth = StubTqAuth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tqsdk", module)
    StubTqApi.instances.clear()
    StubTqAuth.instances.clear()
    return StubTqApi  # 返回桩类便于断言


def preset_rows(monkeypatch: Any, rows: list[dict[str, Any]]) -> None:
    """让每个新建的桩实例都返回指定行（须在 install 之后调用）。"""
    original_init = StubTqApi.__init__

    def init_with_rows(self: StubTqApi, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.rows = rows

    monkeypatch.setattr(StubTqApi, "__init__", init_with_rows)


def klines_rows(days: list[str]) -> list[dict[str, Any]]:
    """按官方文档构造输出行：datetime 纳秒 + open_oi/close_oi 持仓量。"""
    return [
        {
            "id": index,
            "datetime": _to_ns(day),
            "open": 3000.0,
            "high": 3010.0,
            "low": 2990.0,
            "close": 3005.0,
            "volume": 500,
            "open_oi": 100000,
            "close_oi": 101000,
        }
        for index, day in enumerate(days)
    ]


def sized_klines_rows(n: int, *, start_day: str = "2026-01-01") -> list[dict[str, Any]]:
    """n 根 K 线的输出行（分钟步进），用于上游截断/覆盖边界测试。"""
    base = datetime.fromisoformat(start_day).replace(tzinfo=timezone.utc)
    return [
        {
            "id": index,
            "datetime": int((base.timestamp() + index * 60) * 1_000_000_000),
            "open": 3000.0,
            "high": 3010.0,
            "low": 2990.0,
            "close": 3005.0,
            "volume": 500,
            "open_oi": 100000,
            "close_oi": 101000,
        }
        for index in range(n)
    ]
