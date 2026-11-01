"""测试用 akshare SDK 桩：模拟延迟导入的 ``akshare`` 模块。

只实现 tapehub 用到的接口，记录调用参数并返回确定性数据行（dict 列表），
不发起任何网络请求。数据行结构按官方文档核实过的输出列构造。
"""

from typing import Any


class StubDataFrame:
    """最小 DataFrame 桩：只暴露适配器使用的 ``to_dict("records")``。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str | None = None) -> list[dict[str, Any]]:
        return list(self._rows)


class StubAkshare:
    """akshare 模块桩，记录每次调用的 (接口名, 参数)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def set_rows(self, operation: str, rows: list[dict[str, Any]]) -> None:
        self.rows[operation] = rows

    def _respond(self, operation: str, **kwargs: Any) -> StubDataFrame:
        self.calls.append((operation, dict(kwargs)))
        return StubDataFrame(self.rows.get(operation, []))

    # 个股
    def stock_zh_a_hist(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_a_hist", **kwargs)

    def stock_zh_a_daily(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_a_daily", **kwargs)

    def stock_zh_a_hist_tx(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_a_hist_tx", **kwargs)

    # 指数
    def stock_zh_index_daily_em(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_index_daily_em", **kwargs)

    def stock_zh_index_daily(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_index_daily", **kwargs)

    def stock_zh_index_daily_tx(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_index_daily_tx", **kwargs)

    # 期货
    def futures_zh_daily_sina(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("futures_zh_daily_sina", **kwargs)


def em_stock_rows(days: list[str], volume_lots: int = 12345) -> list[dict[str, Any]]:
    """stock_zh_a_hist 输出行：中文列名，成交量单位为手。"""
    return [
        {
            "日期": day,
            "股票代码": "600000",
            "开盘": 10.0,
            "收盘": 10.2,
            "最高": 10.3,
            "最低": 9.9,
            "成交量": volume_lots,
            "成交额": 12345678.0,
            "振幅": 4.0,
            "涨跌幅": 2.0,
            "涨跌额": 0.2,
            "换手率": 1.5,
        }
        for day in days
    ]


def stock_rows_en(days: list[str]) -> list[dict[str, Any]]:
    """stock_zh_a_daily / stock_zh_a_hist_tx 输出行：英文列名，成交量单位为股。"""
    return [
        {
            "date": day,
            "open": 10.0,
            "high": 10.3,
            "low": 9.9,
            "close": 10.2,
            "volume": 1234500,
            "amount": 12345678.0,
        }
        for day in days
    ]


def sina_index_rows(days: list[str]) -> list[dict[str, Any]]:
    """stock_zh_index_daily 输出行：无成交额列。"""
    return [
        {"date": day, "open": 3000.0, "high": 3010.0, "low": 2990.0, "close": 3005.0, "volume": 1}
        for day in days
    ]


def tx_index_rows(days: list[str]) -> list[dict[str, Any]]:
    """stock_zh_index_daily_tx 输出行：amount 列语义为成交量（手）。"""
    return [
        {"date": day, "open": 3000.0, "close": 3005.0, "high": 3010.0, "low": 2990.0, "amount": 999}
        for day in days
    ]


def sina_futures_rows(days: list[str]) -> list[dict[str, Any]]:
    """futures_zh_daily_sina 输出行。"""
    return [
        {
            "date": day,
            "open": 4000.0,
            "high": 4010.0,
            "low": 3990.0,
            "close": 4005.0,
            "volume": 54321,
            "hold": 100000,
            "settle": 4004.0,
        }
        for day in days
    ]
