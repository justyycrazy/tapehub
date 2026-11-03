"""探针分析算法测试（离线，合成 fetch 闭包构造已知数据）。"""

import asyncio
import datetime as dt

import pytest
from probes.analysis import (
    compare_price_series,
    null_rates,
    run_history_depth,
    window_back,
)

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Bar

UTC = dt.timezone.utc
DATA_START = dt.datetime(2003, 4, 10, tzinfo=UTC)
SEARCH_END = dt.datetime(2026, 1, 1, tzinfo=UTC)


def _bar(day: dt.datetime, close: float | None = 10.0, amount: float | None = 1.0) -> Bar:
    return Bar(
        timestamp=dt.datetime(day.year, day.month, day.day, tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        amount=amount,
    )


def test_null_rates() -> None:
    bars = [
        _bar(dt.datetime(2025, 1, 1, tzinfo=UTC)),
        _bar(dt.datetime(2025, 1, 2, tzinfo=UTC), amount=None),
    ]
    rates = null_rates(bars, ["close", "amount"])
    assert rates == {"close": 0.0, "amount": 0.5}
    assert null_rates([], ["close"]) == {"close": 0.0}


def test_compare_price_series() -> None:
    day = dt.datetime(2025, 1, 1, tzinfo=UTC)
    same = [_bar(day, close=10.0), _bar(dt.datetime(2025, 1, 2, tzinfo=UTC), close=11.0)]
    identical = compare_price_series(same, list(same), "close")
    assert identical.identical and identical.compared == 2

    adjusted = [_bar(day, close=9.5), _bar(dt.datetime(2025, 1, 2, tzinfo=UTC), close=10.4)]
    differ = compare_price_series(same, adjusted, "close")
    assert not differ.identical
    assert differ.max_abs_diff == pytest.approx(0.6)

    # 无重叠时间戳 → compared == 0
    other_day = [_bar(dt.datetime(2024, 1, 1, tzinfo=UTC))]
    empty = compare_price_series(same, other_day, "close")
    assert empty.compared == 0


def test_window_back_calendar_and_duration() -> None:
    end = dt.datetime(2025, 3, 31, 12, tzinfo=UTC)
    assert window_back(end, Interval.parse("P1Y")) == dt.datetime(2024, 3, 31, 12, tzinfo=UTC)
    assert window_back(end, Interval.parse("P1M")) == dt.datetime(2025, 2, 28, 12, tzinfo=UTC)
    assert window_back(end, Interval.parse("P1D")) == dt.datetime(2025, 3, 30, 12, tzinfo=UTC)
    assert window_back(end, Interval.parse("PT1H")) == dt.datetime(2025, 3, 31, 11, tzinfo=UTC)


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
            earliest=dt.datetime(1991, 1, 1, tzinfo=UTC),
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
            earliest=dt.datetime(1991, 1, 1, tzinfo=UTC),
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
            earliest=dt.datetime(1991, 1, 1, tzinfo=UTC),
            latest=SEARCH_END,
            max_calls=3,
        )
    )
    assert not result.complete
    assert result.calls_used == 3
    assert result.probes  # 已记录部分探测轨迹
