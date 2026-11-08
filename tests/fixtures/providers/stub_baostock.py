"""测试用 baostock SDK 桩：模拟延迟导入的 ``baostock`` 模块。

只实现 tapehub 用到的模块级函数（login/logout/query_history_k_data_plus），
记录调用参数并返回确定性结果集，不发起任何网络请求。结果集行为与官方
ResultData 对齐：行值全为字符串、缺失为空串 ""、经 next()/get_row_data()
分批取数、error_code/error_msg 字符串返回（"0"=成功，不抛异常）。
"""

from typing import Any


class StubResultData:
    """最小 ResultData 桩：fields + next()/get_row_data() 分批取数语义。"""

    def __init__(
        self,
        fields: list[str],
        rows: list[list[str]],
        *,
        error_code: str = "0",
        error_msg: str = "",
    ) -> None:
        self.fields = list(fields)
        self._rows = [list(row) for row in rows]
        self._cursor = 0
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self) -> bool:
        if self._cursor >= len(self._rows):
            return False
        self._cursor += 1
        return True

    def get_row_data(self) -> list[str]:
        """当前行（原值字符串）；调用前必须先 next() 成功。"""
        return list(self._rows[self._cursor - 1])

    def get_data(self) -> list[list[str]]:
        """全量取数（探针 raw 链使用；桩返回行列表，非 DataFrame）。"""
        return [list(row) for row in self._rows]


class StubBaostock:
    """baostock 模块桩，记录每次调用的 (函数名, 参数)。

    - ``login_error_code``/``login_error_msg``：非 "0" 时 login 返回失败；
    - ``pending_errors``：查询结果队列前置的错误结果，按次消费——用于
      「先错后对」的会话失效重登重试脚本；耗尽后回落到 ``result``；
    - ``result``：每次查询的默认结果；未预置时抛 AssertionError（防静默空转）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.login_error_code = "0"
        self.login_error_msg = ""
        self.pending_errors: list[StubResultData] = []
        self.result: StubResultData | None = None
        self.login_count = 0
        self.logout_count = 0

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, dict(kwargs)))

    def login(self, **kwargs: Any) -> StubResultData:
        self.login_count += 1
        self._record("login", kwargs)
        return StubResultData(
            [], [], error_code=self.login_error_code, error_msg=self.login_error_msg
        )

    def logout(self, **kwargs: Any) -> StubResultData:
        self.logout_count += 1
        self._record("logout", kwargs)
        return StubResultData([], [], error_code="0")

    def query_history_k_data_plus(self, **kwargs: Any) -> StubResultData:
        self._record("query_history_k_data_plus", kwargs)
        if self.pending_errors:
            return self.pending_errors.pop(0)
        if self.result is None:
            raise AssertionError("stub has no preset query result")
        return self.result


# baostock query_history_k_data_plus 的字段名（官方 fields 集合顺序）
DAILY_STOCK_FIELDS = [
    "date", "code", "open", "high", "low", "close", "preclose", "volume",
    "amount", "adjustflag", "turn", "tradestatus", "pctChg", "isST",
    "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM",
]
WEEKLY_MONTHLY_FIELDS = [
    "date", "code", "open", "high", "low", "close", "volume", "amount",
    "adjustflag", "turn", "pctChg",
]
MINUTE_FIELDS = [
    "date", "time", "code", "open", "high", "low", "close", "volume", "amount", "adjustflag",
]


def daily_stock_rows(days: list[str]) -> StubResultData:
    """日线结果集：字符串行值、成交量单位股；缺失字段为空串。"""
    rows = [
        [
            day,            # date
            "sh.600000",    # code
            "12.6400",      # open
            "12.6500",      # high
            "12.4700",      # low
            "12.5600",      # close
            "12.6500",      # preclose
            "38778949",     # volume（股）
            "486264672",    # amount（元）
            "2",            # adjustflag
            "0.137985",     # turn（%）
            "1",            # tradestatus
            "-0.711456",    # pctChg（%）
            "0",            # isST
            "5.123456",     # peTTM
            "0.456789",     # pbMRQ
            "1.234567",     # psTTM
            "",             # pcfNcfTTM（缺失 → null）
        ]
        for day in days
    ]
    return StubResultData(DAILY_STOCK_FIELDS, rows)


def daily_suspended_rows(days: list[str]) -> StubResultData:
    """停牌日结果集：OHLC=前收、量额=0、换手率空串。"""
    rows = [
        [
            day, "sh.600000", "12.6500", "12.6500", "12.6500", "12.6500",
            "12.6500", "0", "0", "3", "", "0", "0.000000", "0",
            "5.123456", "0.456789", "1.234567", "",
        ]
        for day in days
    ]
    return StubResultData(DAILY_STOCK_FIELDS, rows)


def weekly_rows(weeks: list[str]) -> StubResultData:
    """周/月线结果集：无 preclose/tradestatus/isST/估值字段。"""
    rows = [
        [day, "sh.600000", "12.5000", "12.8000", "12.4000", "12.6000",
         "387789490", "4862646720", "2", "1.379850", "0.711456"]
        for day in weeks
    ]
    return StubResultData(WEEKLY_MONTHLY_FIELDS, rows)


def minute_rows(bars: list[tuple[str, str]]) -> StubResultData:
    """分钟线结果集：(date, time) 对；time 为 YYYYMMDDHHMMSSsss 17 位。"""
    rows = [
        [day, time_text, "sh.600000", "12.6400", "12.6500", "12.6300",
         "12.6450", "387789", "4862646.72", "3"]
        for day, time_text in bars
    ]
    return StubResultData(MINUTE_FIELDS, rows)


def index_rows(days: list[str]) -> StubResultData:
    """指数日线结果集：preclose/pctChg 有值、无估值字段。"""
    fields = ["date", "code", "open", "high", "low", "close", "preclose",
              "volume", "amount", "adjustflag", "pctChg"]
    rows = [
        [day, "sh.000001", "3105.3080", "3136.4550", "3105.3080",
         "3135.9200", "3103.6370", "14156718592", "159887138816.0000",
         "3", "1.040200"]
        for day in days
    ]
    return StubResultData(fields, rows)
