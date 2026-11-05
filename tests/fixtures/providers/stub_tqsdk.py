"""测试用 TqSdk SDK 桩：模拟延迟导入的 ``tqsdk`` 模块。

实现适配器使用的 ``TqApi(auth=TqAuth(user, pwd))`` 路径：
``get_kline_serial(...)``（列式行 + to_dict 桩 DataFrame）与
``get_quote(symbol)``（同步阻塞至首包的真实语义在桩中简化为直接返回
预设快照 dict），记录构造与调用参数，无真实网络。
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

    def __init__(self, user: str = None, password: str = None) -> None:  # type: ignore[assignment]
        self.user = user
        self.password = password
        StubTqAuth.instances.append(self)


# 快照默认字段（P5 探针 20261104T012958Z 的 au2612 实测样本节选）
DEFAULT_QUOTE_FIELDS: dict[str, Any] = {
    "datetime": "2026-11-04 10:14:46.500000",
    "last_price": 848.24,
    "ask_price1": 848.3, "ask_volume1": 2,
    "bid_price1": 848.26, "bid_volume1": 2,
    "ask_price2": 848.36, "ask_volume2": 8,
    "bid_price2": 848.24, "bid_volume2": 12,
    "ask_price3": 848.42, "ask_volume3": 4,
    "bid_price3": 848.22, "bid_volume3": 1,
    "ask_price4": 848.58, "ask_volume4": 4,
    "bid_price4": 848.18, "bid_volume4": 1,
    "ask_price5": 848.6, "ask_volume5": 3,
    "bid_price5": 848.16, "bid_volume5": 1,
    "highest": 853.64, "lowest": 843.14, "open": 846.0,
    "close": float("nan"), "average": 848.47,
    "volume": 200250, "amount": 169906871060.0,
    "open_interest": 160306, "pre_open_interest": 160467,
    "settlement": float("nan"), "upper_limit": 964.56, "lower_limit": 727.66,
    "pre_settlement": 846.12, "pre_close": 841.8,
}


class StubTqApi:
    """TqApi 客户端桩；所有实例记录在 ``instances``。"""

    instances: list["StubTqApi"] = []
    # get_quote 返回的快照字段（preset_quote 覆盖）
    quote_fields: dict[str, Any] = dict(DEFAULT_QUOTE_FIELDS)

    def __init__(self, auth: Any = None, **kwargs: Any) -> None:
        self.auth = auth
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.quote_calls: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.closed = False
        StubTqApi.instances.append(self)

    def get_kline_serial(self, **kwargs: Any) -> StubDataFrame:
        self.calls.append(dict(kwargs))
        return StubDataFrame(self.rows)

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """同步路径（per_request）：直接返回预设首包快照（dict-like）。"""
        self.quote_calls.append(symbol)
        return dict(type(self).quote_fields)

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
    StubTqApi.quote_fields = dict(DEFAULT_QUOTE_FIELDS)
    return StubTqApi  # 返回桩类便于断言


def preset_quote(monkeypatch: Any, fields: dict[str, Any]) -> None:
    """让 get_quote 返回指定快照字段（须在 install 之后调用）。"""
    monkeypatch.setattr(StubTqApi, "quote_fields", dict(fields))


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
