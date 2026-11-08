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

    # ETF（东财，与个股同端点同列）
    def fund_etf_hist_em(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("fund_etf_hist_em", **kwargs)

    # 港股/美股（东财，与个股同端点同列）
    def stock_hk_hist(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_hk_hist", **kwargs)

    def stock_us_hist(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_us_hist", **kwargs)

    def stock_zh_a_daily(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_a_daily", **kwargs)

    def stock_zh_a_hist_tx(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("stock_zh_a_hist_tx", **kwargs)

    # 指数
    def index_zh_a_hist(self, **kwargs: Any) -> StubDataFrame:
        return self._respond("index_zh_a_hist", **kwargs)

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


def em_etf_rows(days: list[str], volume_lots: int = 23456) -> list[dict[str, Any]]:
    """fund_etf_hist_em 输出行：中文列名（无「股票代码」列），成交量单位为手。"""
    return [
        {
            "日期": day,
            "开盘": 3.1,
            "收盘": 3.15,
            "最高": 3.18,
            "最低": 3.08,
            "成交量": volume_lots,
            "成交额": 9876543.0,
            "振幅": 3.2,
            "涨跌幅": 1.6,
            "涨跌额": 0.05,
            "换手率": 0.9,
        }
        for day in days
    ]


def em_index_rows(days: list[str], volume_lots: int = 561375) -> list[dict[str, Any]]:
    """index_zh_a_hist 输出行：中文列名（无「股票代码」列），成交量单位为手
    （原样透出，桩值即手量级，用于断言不发生 ×100 换算）。"""
    return [
        {
            "日期": day,
            "开盘": 3300.0,
            "收盘": 3262.56,
            "最高": 3351.72,
            "最低": 3242.09,
            "成交量": volume_lots,
            "成交额": 603340526739.8,
            "振幅": 3.36,
            "涨跌幅": -1.58,
            "涨跌额": -52.31,
            "换手率": 0.0,
        }
        for day in days
    ]


def em_global_rows(
    days: list[str], *, close: float = 88.5, volume: int = 345678
) -> list[dict[str, Any]]:
    """stock_hk_hist / stock_us_hist 输出行：中文列名（无「股票代码」列），
    成交量原样透出（单位待探针定标，桩不预设）。"""
    return [
        {
            "日期": day,
            "开盘": close - 0.5,
            "收盘": close,
            "最高": close + 0.8,
            "最低": close - 0.7,
            "成交量": volume,
            "成交额": 8765432.0,
            "振幅": 1.7,
            "涨跌幅": 0.6,
            "涨跌额": 0.5,
            "换手率": 0.3,
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
