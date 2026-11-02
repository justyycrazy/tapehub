"""测试用 Tushare SDK 桩：模拟延迟导入的 ``tushare`` 模块。

只实现适配器使用的 ``ts.pro_api(token)``、``ts.pro_bar(...)`` 与
``pro.fut_daily(...)`` 路径，记录调用参数并返回文档核实的输出行
（dict 列表 + to_dict("records") 桩 DataFrame），无真实网络。
"""

from typing import Any


def _date_str(day: str) -> str:
    return day.replace("-", "")


class StubDataFrame:
    """最小 DataFrame 桩：只暴露适配器使用的 ``to_dict("records")``。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str | None = None) -> list[dict[str, Any]]:
        return list(self._rows)


class StubPro:
    """ts.pro_api 返回的 pro 客户端桩。"""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def set_rows(self, operation: str, rows: list[dict[str, Any]]) -> None:
        self.rows[operation] = rows

    def fut_daily(self, **kwargs: Any) -> StubDataFrame:
        self.calls.append(("fut_daily", dict(kwargs)))
        return StubDataFrame(self.rows.get("fut_daily", []))


class StubTushareModule:
    """tushare 模块桩：pro_api + 模块级 pro_bar。

    ``set_rows`` 可在 pro 实例创建前预置响应，创建实例时自动继承。
    """

    def __init__(self) -> None:
        self.pro_instances: list[StubPro] = []
        self.pro_bar_calls: list[dict[str, Any]] = []
        self.pro_bar_rows: list[dict[str, Any]] = []
        self._pending: dict[str, list[dict[str, Any]]] = {}

    def set_rows(self, operation: str, rows: list[dict[str, Any]]) -> None:
        self._pending[operation] = rows

    def pro_api(self, token: str | None = None) -> StubPro:
        pro = StubPro(token)
        pro.rows = dict(self._pending)
        self.pro_instances.append(pro)
        return pro

    def pro_bar(self, **kwargs: Any) -> StubDataFrame:
        self.pro_bar_calls.append(dict(kwargs))
        return StubDataFrame(self.pro_bar_rows)

    @property
    def last_pro(self) -> StubPro:
        return self.pro_instances[-1]


def install(monkeypatch: Any) -> StubTushareModule:
    import sys

    module = StubTushareModule()
    monkeypatch.setitem(sys.modules, "tushare", module)
    return module


def stock_rows(days: list[str]) -> list[dict[str, Any]]:
    """pro_bar（daily）输出行：中文单位口径——vol 手、amount 千元。"""
    return [
        {
            "ts_code": "600000.SH",
            "trade_date": _date_str(day),
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.2,
            "pre_close": 10.1,
            "change": 0.1,
            "pct_chg": 1.0,
            "vol": 12345.0,
            "amount": 12345.0,
        }
        for day in days
    ]


def futures_rows(days: list[str]) -> list[dict[str, Any]]:
    """fut_daily 输出行：vol 手、amount 万元、oi 手。"""
    return [
        {
            "ts_code": "CU2401.SHF",
            "trade_date": _date_str(day),
            "pre_close": 68100.0,
            "pre_settle": 68150.0,
            "open": 68200.0,
            "high": 68400.0,
            "low": 68000.0,
            "close": 68300.0,
            "settle": 68250.0,
            "vol": 54321.0,
            "amount": 37100.0,
            "oi": 100000.0,
        }
        for day in days
    ]
