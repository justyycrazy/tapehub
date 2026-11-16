"""测试用 TqSdk Runtime 假 SDK（shared 模式线程驱动语义，零网络零凭据）。

模拟 tqsdk 的同步驱动模型（Runtime 线程 tick 驱动）：

- ``create_task`` 把协程挂到私有循环；
- ``get_kline_serial`` 返回可 await 的序列对象（任务推进若干 tick 后完成）；
- ``get_quote`` 返回可 await 的 dict-like Quote（推进若干 tick 收到首包；
  循环未运行时同步驱动至首包，模拟真实 SDK 的阻塞语义）；
- ``wait_update(deadline)`` 推进私有循环一小段，按脚本推进行情首包/增量
  （``_changed_ids`` 供 ``is_changing`` 判定），可注入连续故障（watchdog）。

模块级旋钮（测试按需覆盖）：

- ``FETCH_DELAY`` / ``TICKS_PER_FETCH``：取数协程推进时长与 tick 数；
- ``FakeTqApi.fail_construct`` / ``fail_wait_update_times``：故障注入；
- ``FakeTqApi.construct_fail_times``：前 N 次 TqApi 构造抛错（暂时性失败序列）；
- ``FakeTqApi.construct_hang_seconds`` / ``construct_hang_times``：前 N 次
  构造先阻塞该时长（构造挂死/慢构造序列）；
- ``FakeTqApi.quote_first_packet_ticks``：首包到达所需 tick 数；
- ``FakeTqApi.quote_update_every_ticks``：增量推送周期（0 = 不推送）。
"""

import asyncio
import sys
import time
import types
from typing import Any

# 取数协程推进所需时长（秒）；闸门/停机测试通过调大它制造慢请求
FETCH_DELAY = 0.0
TICKS_PER_FETCH = 2
# 构造挂死时长（秒）：construct_hang_times 次数内生效
CONSTRUCT_HANG_SECONDS = 0.3


def _quote_fields(last_price: float) -> dict[str, object]:
    """首包快照字段（取自 P5 探针 20261104T012958Z 的 au2612 实测样本）。"""
    return {
        "datetime": "2026-11-04 10:14:46.500000",
        "last_price": last_price,
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


class FakeQuote(dict):
    """dict-like Quote：datetime 非空即首包已到；可 await（协程用法）。"""

    def __await__(self):  # type: ignore[no-untyped-def]
        async def _go():  # type: ignore[no-untyped-def]
            while self.get("datetime", "") == "":
                await asyncio.sleep(0.005)
            return self

        return _go().__await__()


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dict(self, kind: str) -> list[dict[str, object]]:
        return list(self._rows)


class FakeSerial:
    """可 await 序列：推进 ticks 次（每次需 wait_update 驱动）后返回 frame。

    完成时回调 ``api`` 归还在飞取数计数（finally 保证取消/异常路径也归还）。
    """

    def __init__(self, api: "FakeTqApi", rows: list[dict[str, object]], ticks: int) -> None:
        self._api = api
        self._rows = rows
        self._ticks = ticks

    def __await__(self):  # type: ignore[no-untyped-def]
        async def _go():  # type: ignore[no-untyped-def]
            try:
                for _ in range(self._ticks):
                    await asyncio.sleep(FETCH_DELAY / max(1, self._ticks))
                return FakeFrame(self._rows)
            finally:
                self._api._inflight_fetches = max(0, self._api._inflight_fetches - 1)

        return _go().__await__()


class FakeTqApi:
    instances: list["FakeTqApi"] = []
    fail_construct = False
    fail_wait_update_times = 0
    # 暂时性构造失败/挂死序列：每次构造消费一格（前 N 次生效）
    construct_fail_times = 0
    construct_hang_times = 0
    rows_per_call = 3
    # quote 语义脚本：首包在第 N 次 wait_update 到达；其后每 N 次推送一次增量
    quote_first_packet_ticks = 2
    quote_update_every_ticks = 0

    def __init__(
        self, auth: object = None, disable_print: bool = False, loop: object = None, **kw: object
    ) -> None:
        if FakeTqApi.fail_construct:
            raise RuntimeError("auth failed")
        if FakeTqApi.construct_fail_times > 0:
            FakeTqApi.construct_fail_times -= 1
            raise RuntimeError("transient construction failure")
        if FakeTqApi.construct_hang_times > 0:
            FakeTqApi.construct_hang_times -= 1
            time.sleep(CONSTRUCT_HANG_SECONDS)
        self.auth = auth
        self._loop = (
            loop if isinstance(loop, asyncio.AbstractEventLoop) else asyncio.new_event_loop()
        )
        self._tasks: list[asyncio.Task] = []
        self.closed = False
        self._tick_count = 0
        self._quotes: dict[str, FakeQuote] = {}
        self._quote_first_due: dict[str, int] = {}
        self._changed_ids: set[int] = set()
        self.get_quote_calls: list[str] = []
        # 在飞取数观测：get_kline_serial 已调用且序列未完成（协程已进入 await）
        self._inflight_fetches = 0
        self.inflight_peak = 0
        FakeTqApi.instances.append(self)

    def create_task(self, coro):  # type: ignore[no-untyped-def]
        task = self._loop.create_task(coro)
        self._tasks.append(task)
        return task

    def get_kline_serial(self, **kwargs: object) -> FakeSerial:
        # datetime 为 ns 时间戳（2026-01-02 00:00 +08:00 起，逐根 +1 日）
        base_ns = 1767254400000000000
        day_ns = 86400 * 1_000_000_000
        rows = [
            {
                "id": i,
                "symbol": kwargs.get("symbol"),
                "datetime": base_ns + i * day_ns,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
            }
            for i in range(FakeTqApi.rows_per_call)
        ]
        self._inflight_fetches += 1
        self.inflight_peak = max(self.inflight_peak, self._inflight_fetches)
        return FakeSerial(self, rows, ticks=TICKS_PER_FETCH)

    def get_quote(self, symbol: str) -> FakeQuote:
        """订阅并返回 Quote；循环未运行（同步路径）时驱动至首包（真实 SDK 语义）。"""
        self.get_quote_calls.append(symbol)
        if symbol not in self._quotes:
            quote = FakeQuote({"datetime": "", "last_price": float("nan")})
            self._quotes[symbol] = quote
            self._quote_first_due[symbol] = max(1, FakeTqApi.quote_first_packet_ticks)
        if not self._loop.is_running():
            guard = 0
            while self._quotes[symbol].get("datetime", "") == "":
                self.wait_update()
                guard += 1
                if guard > 1000:  # 防御：脚本配置异常时避免死循环
                    raise RuntimeError("fake get_quote never received first packet")
        return self._quotes[symbol]

    def is_changing(self, obj: object) -> bool:
        return id(obj) in self._changed_ids

    def wait_update(self, deadline: float | None = None) -> bool:
        if FakeTqApi.fail_wait_update_times > 0:
            FakeTqApi.fail_wait_update_times -= 1
            raise RuntimeError("connection broken")
        self._changed_ids = set()
        self._tick_count += 1
        for symbol, quote in self._quotes.items():
            if quote.get("datetime", "") == "":
                due = self._quote_first_due.get(symbol, 0) - 1
                self._quote_first_due[symbol] = due
                if due <= 0:
                    quote.update(_quote_fields(848.24))
                    self._changed_ids.add(id(quote))
            elif (
                FakeTqApi.quote_update_every_ticks > 0
                and self._tick_count % FakeTqApi.quote_update_every_ticks == 0
            ):
                quote["last_price"] = float(quote.get("last_price", 0.0)) + 1.0
                self._changed_ids.add(id(quote))
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            self._loop.run_until_complete(asyncio.wait(pending, timeout=0.05))
        else:
            time.sleep(0.005)
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for task in self._tasks:
            task.cancel()
        try:
            if self._tasks:  # 空 gather 在无当前循环的线程里会取事件循环，需规避
                self._loop.run_until_complete(
                    asyncio.gather(*self._tasks, return_exceptions=True)
                )
        finally:
            self._loop.close()


def install(monkeypatch: Any) -> types.ModuleType:
    """把假 SDK 模块注入 sys.modules 并重置全部脚本旋钮。"""
    module = types.ModuleType("tqsdk")

    class FakeTqAuth:
        def __init__(self, user_name: str = "", password: str = "") -> None:
            self.user_name = user_name
            self.password = password

    module.TqApi = FakeTqApi  # type: ignore[attr-defined]
    module.TqAuth = FakeTqAuth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tqsdk", module)
    FakeTqApi.instances = []
    FakeTqApi.fail_construct = False
    FakeTqApi.fail_wait_update_times = 0
    FakeTqApi.construct_fail_times = 0
    FakeTqApi.construct_hang_times = 0
    FakeTqApi.quote_first_packet_ticks = 2
    FakeTqApi.quote_update_every_ticks = 0
    global FETCH_DELAY, TICKS_PER_FETCH
    FETCH_DELAY = 0.0
    TICKS_PER_FETCH = 2
    return module
