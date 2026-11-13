"""探针分析算法测试（离线，合成 fetch 闭包构造已知数据）。

桩数据统一北京时间（+08:00）；「算法不依赖特定时区」的属性由
``test_analysis_utc.py`` 以 UTC 桩单独守护。
"""

import asyncio
import datetime as dt

import pytest
from probes.analysis import (
    compare_price_series,
    null_rates,
    run_history_depth,
    run_history_depth_full,
    window_back,
)

from tapehub.domain.intervals import Interval
from tapehub.domain.models import CN_TIMEZONE, Bar

DATA_START = dt.datetime(2003, 4, 10, tzinfo=CN_TIMEZONE)
SEARCH_END = dt.datetime(2026, 1, 1, tzinfo=CN_TIMEZONE)


def _bar(day: dt.datetime, close: float | None = 10.0, amount: float | None = 1.0) -> Bar:
    return Bar(
        timestamp=dt.datetime(day.year, day.month, day.day, tzinfo=CN_TIMEZONE),
        open=close,
        high=close,
        low=close,
        close=close,
        amount=amount,
    )


def test_null_rates() -> None:
    bars = [
        _bar(dt.datetime(2025, 1, 1, tzinfo=CN_TIMEZONE)),
        _bar(dt.datetime(2025, 1, 2, tzinfo=CN_TIMEZONE), amount=None),
    ]
    rates = null_rates(bars, ["close", "amount"])
    assert rates == {"close": 0.0, "amount": 0.5}
    assert null_rates([], ["close"]) == {"close": 0.0}


def test_compare_price_series() -> None:
    day = dt.datetime(2025, 1, 1, tzinfo=CN_TIMEZONE)
    same = [_bar(day, close=10.0), _bar(dt.datetime(2025, 1, 2, tzinfo=CN_TIMEZONE), close=11.0)]
    identical = compare_price_series(same, list(same), "close")
    assert identical.identical and identical.compared == 2

    adjusted = [_bar(day, close=9.5), _bar(dt.datetime(2025, 1, 2, tzinfo=CN_TIMEZONE), close=10.4)]
    differ = compare_price_series(same, adjusted, "close")
    assert not differ.identical
    assert differ.max_abs_diff == pytest.approx(0.6)

    # 无重叠时间戳 → compared == 0
    other_day = [_bar(dt.datetime(2024, 1, 1, tzinfo=CN_TIMEZONE))]
    empty = compare_price_series(same, other_day, "close")
    assert empty.compared == 0


def test_compare_price_series_tolerance() -> None:
    """容差内差异不判分歧，但 max_abs_diff 仍如实记录全部差异幅度。"""
    day = dt.datetime(2025, 1, 1, tzinfo=CN_TIMEZONE)
    a = [_bar(day, close=10.0), _bar(dt.datetime(2025, 1, 2, tzinfo=CN_TIMEZONE), close=11.0)]
    b = [_bar(day, close=10.005), _bar(dt.datetime(2025, 1, 2, tzinfo=CN_TIMEZONE), close=11.004)]

    within = compare_price_series(a, b, "close", tolerance=0.01)
    assert within.identical
    assert within.first_divergence is None
    assert within.max_abs_diff == pytest.approx(0.005)
    assert within.tolerance == pytest.approx(0.01)

    beyond = compare_price_series(a, b, "close", tolerance=0.001)
    assert not beyond.identical
    assert beyond.first_divergence == day


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("P1Y", (2024, 3, 31, 12)),
        ("P1M", (2025, 2, 28, 12)),
        ("P1D", (2025, 3, 30, 12)),
        ("PT1H", (2025, 3, 31, 11)),
    ],
)
def test_window_back_calendar_and_duration(interval: str, expected: tuple[int, ...]) -> None:
    end = dt.datetime(2025, 3, 31, 12, tzinfo=CN_TIMEZONE)
    assert window_back(end, Interval.parse(interval)) == dt.datetime(*expected, tzinfo=CN_TIMEZONE)


def _fetch_from_data_start():
    """合成 fetch：数据自 DATA_START 起连续存在，窗口与之相交即返回一根 bar。"""

    async def fetch(start: dt.datetime, end: dt.datetime) -> list[Bar]:
        if end > DATA_START and start < SEARCH_END:
            return [_bar(max(start, DATA_START))]
        return []

    return fetch


def test_history_depth_finds_earliest() -> None:
    result = asyncio.run(
        run_history_depth(
            _fetch_from_data_start(),
            earliest=dt.datetime(1991, 1, 1, tzinfo=CN_TIMEZONE),
            latest=SEARCH_END,
            era_step_years=5,
            max_calls=40,
        )
    )
    assert result.complete
    assert result.earliest_estimate is not None
    assert abs((result.earliest_estimate - DATA_START).days) <= 90


def test_history_depth_no_data() -> None:
    async def fetch(start: dt.datetime, end: dt.datetime) -> list[Bar]:
        return []

    result = asyncio.run(
        run_history_depth(
            fetch,
            earliest=dt.datetime(1991, 1, 1, tzinfo=CN_TIMEZONE),
            latest=SEARCH_END,
            max_calls=40,
        )
    )
    assert result.complete
    assert result.earliest_estimate is None


def test_history_depth_respects_budget() -> None:
    result = asyncio.run(
        run_history_depth(
            _fetch_from_data_start(),
            earliest=dt.datetime(1991, 1, 1, tzinfo=CN_TIMEZONE),
            latest=SEARCH_END,
            max_calls=3,
        )
    )
    assert not result.complete
    assert result.calls_used == 3
    assert result.probes  # 已记录部分探测轨迹


def test_history_depth_bisect_stops_at_span() -> None:
    """二分收敛到窗口粒度即止：估算精度不变，调用数从对数(区间/1天)降为对数(区间/窗口)。"""
    calls = 0
    base = _fetch_from_data_start()

    async def counting_fetch(start: dt.datetime, end: dt.datetime) -> list[Bar]:
        nonlocal calls
        calls += 1
        return await base(start, end)

    result = asyncio.run(
        run_history_depth(
            counting_fetch,
            earliest=dt.datetime(1991, 1, 1, tzinfo=CN_TIMEZONE),
            latest=SEARCH_END,
            era_step_years=5,
            max_calls=40,
        )
    )
    assert result.complete
    assert result.granularity_days == 90
    # 区间 ~35 年：收敛到 1 天约需 13 次二分 + 验证，收敛到 90 天约 6 次
    assert result.calls_used == calls
    assert calls <= 20
    assert abs((result.earliest_estimate - DATA_START).days) <= 90


def test_history_depth_on_step_reports_phases() -> None:
    """on_step 回调按轮次收到探测条目：phase 标签齐全，条目与 probes 记录同源。"""
    steps: list[dict] = []

    result = asyncio.run(
        run_history_depth(
            _fetch_from_data_start(),
            earliest=dt.datetime(1991, 1, 1, tzinfo=CN_TIMEZONE),
            latest=SEARCH_END,
            era_step_years=5,
            max_calls=40,
            on_step=steps.append,
        )
    )
    assert result.complete
    assert steps == result.probes  # 回调收到的就是写入证据的同一批条目
    phases = [entry["phase"] for entry in steps]
    assert set(phases) <= {"era", "bisect", "verify"}
    assert phases[0] == "era" and "bisect" in phases  # 先纪元回溯，后二分
    assert phases.index("era") < phases.index("bisect")


def test_history_depth_full_mode_reads_exact_earliest() -> None:
    """全量模式：单次调用直接返回精确最早 bar，无粒度误差。"""

    async def fetch(start: dt.datetime, end: dt.datetime) -> list[Bar]:
        if end > DATA_START and start < SEARCH_END:
            return [_bar(max(start, DATA_START))]
        return []

    result = asyncio.run(
        run_history_depth_full(
            fetch,
            earliest=dt.datetime(1991, 1, 1, tzinfo=CN_TIMEZONE),
            latest=SEARCH_END,
        )
    )
    assert result.complete
    assert result.approx is False
    assert result.calls_used == 1
    assert result.earliest_estimate == DATA_START  # 精确值，不是窗口估算
    assert result.probes[0]["phase"] == "full"


def test_history_depth_full_mode_empty_range_is_exact_answer() -> None:
    """全量模式单次调用覆盖全区间，「区间内无数据」也是精确结论。"""

    async def fetch(start: dt.datetime, end: dt.datetime) -> list[Bar]:
        return []

    result = asyncio.run(
        run_history_depth_full(
            fetch,
            earliest=dt.datetime(1991, 1, 1, tzinfo=CN_TIMEZONE),
            latest=SEARCH_END,
        )
    )
    assert result.complete
    assert result.approx is False
    assert result.earliest_estimate is None
