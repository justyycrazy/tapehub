"""探针分析算法的时区无关性守护测试（离线）。

项目口径统一北京时间（+08:00），分析算法本身不得依赖特定时区——
本文件刻意用 UTC 桩验证该属性，防止将来有人在算法中引入时区假设
（如硬编码 CN_TIMEZONE、按 tzinfo 分支）。常规行为测试见
``test_analysis.py``（北京时间桩）。
"""

import asyncio
import datetime as dt

from probes.analysis import compare_price_series, run_history_depth, window_back

from tapehub.domain.intervals import Interval
from tapehub.domain.models import CN_TIMEZONE, Bar

UTC = dt.timezone.utc
DATA_START = dt.datetime(2003, 4, 10, tzinfo=UTC)
SEARCH_END = dt.datetime(2026, 1, 1, tzinfo=UTC)


def _bar(at: dt.datetime, close: float = 10.0) -> Bar:
    return Bar(timestamp=at, open=close, high=close, low=close, close=close)


def test_window_back_under_utc() -> None:
    """墙钟回溯在 UTC 下同样正确（年/月按日历回退，其余按时长）。"""
    end = dt.datetime(2025, 3, 31, 12, tzinfo=UTC)
    assert window_back(end, Interval.parse("P1Y")) == dt.datetime(2024, 3, 31, 12, tzinfo=UTC)
    assert window_back(end, Interval.parse("P1M")) == dt.datetime(2025, 2, 28, 12, tzinfo=UTC)
    assert window_back(end, Interval.parse("PT1H")) == dt.datetime(2025, 3, 31, 11, tzinfo=UTC)


def test_compare_aligns_same_instant_across_zones() -> None:
    """同一绝对时刻的 UTC 与北京表达必须对齐（aware datetime 按时刻相等）。"""
    instant = dt.datetime(2025, 1, 1, 16, tzinfo=UTC)  # = 北京 2025-01-02 00:00
    utc_series = [_bar(instant, close=10.0)]
    cn_series = [_bar(instant.astimezone(CN_TIMEZONE), close=10.0)]
    result = compare_price_series(utc_series, cn_series, "close")
    assert result.compared == 1
    assert result.identical


def test_history_depth_preserves_input_zone() -> None:
    """UTC 输入的搜索结果保持 UTC（算法原样保留输入时区，不隐式转换）。"""

    async def fetch(start: dt.datetime, end: dt.datetime) -> list[Bar]:
        if end > DATA_START and start < SEARCH_END:
            return [_bar(max(start, DATA_START))]
        return []

    result = asyncio.run(
        run_history_depth(
            fetch,
            earliest=dt.datetime(1991, 1, 1, tzinfo=UTC),
            latest=SEARCH_END,
            era_step_years=5,
            max_calls=40,
        )
    )
    assert result.complete
    assert result.earliest_estimate is not None
    # 估计值保持 UTC 口径，且绝对时刻与北京时间表达互换相等
    assert result.earliest_estimate.utcoffset() == dt.timedelta(0)
    cn_view = result.earliest_estimate.astimezone(CN_TIMEZONE)
    assert cn_view == result.earliest_estimate
    assert abs((result.earliest_estimate - DATA_START).days) <= 90
