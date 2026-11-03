"""探针分析算法：null 率、复权对比、历史深度、窗口回溯。

所有算法都作用于 ``WindowFetch`` 异步闭包（输入时间窗口、输出标准化 Bar 列表），
不直接依赖适配器，便于离线单测与未来 Worker 复用；调用预算由调用方控制，
预算耗尽抛出 ``BudgetExhausted``。
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Bar

PROBE_SPAN_DAYS = 90  # history-depth 二分搜索的窗口粒度（天）


class BudgetExhausted(Exception):
    """探针调用预算耗尽。"""


class WindowFetch(Protocol):
    """窗口查询闭包：返回窗口内的标准化 Bar（可抛 TapehubError / BudgetExhausted）。"""

    async def __call__(self, start: dt.datetime, end: dt.datetime) -> list[Bar]: ...


def null_rates(bars: list[Bar], fields: list[str]) -> dict[str, float]:
    """逐字段统计 null 占比（空列表返回 0.0，避免除零歧义）。"""
    total = len(bars)
    if total == 0:
        return {name: 0.0 for name in fields}
    return {
        name: sum(getattr(bar, name) is None for bar in bars) / total for name in fields
    }


@dataclass(frozen=True, slots=True)
class SeriesCompare:
    """两序列按 timestamp 对齐后的价格对比结果。"""

    field: str
    compared: int
    identical: bool
    max_abs_diff: float


def compare_price_series(a: list[Bar], b: list[Bar], field_name: str) -> SeriesCompare:
    """按 timestamp 交集对比指定字段；双方任一为 null 的 bar 不参与对比。"""
    left = {bar.timestamp: getattr(bar, field_name) for bar in a}
    compared = 0
    identical = True
    max_diff = 0.0
    for bar in b:
        other = left.get(bar.timestamp)
        value = getattr(bar, field_name)
        if other is None or value is None:
            continue
        compared += 1
        diff = abs(other - value)
        max_diff = max(max_diff, diff)
        if diff > 0:
            identical = False
    return SeriesCompare(
        field=field_name, compared=compared, identical=identical, max_abs_diff=max_diff
    )


@dataclass(slots=True)
class HistoryDepthResult:
    """历史深度探针结果。

    ``earliest_estimate`` 是最早非空窗口的起点，粒度受 ``granularity_days`` 限制，
    因此永远是近似值（``approx=True``）；``complete=False`` 表示预算耗尽提前停止。
    """

    earliest_estimate: dt.datetime | None
    approx: bool
    granularity_days: int | None
    probes: list[dict[str, Any]] = field(default_factory=list)
    calls_used: int = 0
    complete: bool = True


async def run_history_depth(
    fetch: WindowFetch,
    *,
    earliest: dt.datetime,
    latest: dt.datetime,
    era_step_years: int = 5,
    max_calls: int = 24,
    probe_span_days: int = PROBE_SPAN_DAYS,
) -> HistoryDepthResult:
    """纪元回溯 + 二分搜索最早可用窗口。

    前提假设（结果标注 approx）：数据从最早可用点起大体连续。

    1. 按 ``era_step_years`` 年一档自新向旧回溯，扫到第一个**空档**为止；
       最后一个非空档起点记为锚点；
    2. 在「空档起点 ↔ 锚点档终点」之间以 ``probe_span_days`` 天为窗口按天二分，
       收敛到「窗口非空」的最小起点（谓词单调性依赖连续性假设）；
    3. 报告 ``earliest_estimate = 二分收敛点 + 窗口宽度``，误差约为窗口粒度，
       最终做一次验证探测。
    """
    probes: list[dict[str, Any]] = []
    calls = 0

    async def probe(start: dt.datetime, end: dt.datetime) -> bool:
        nonlocal calls
        if calls >= max_calls:
            raise BudgetExhausted()
        calls += 1
        bars = await fetch(start, end)
        probes.append({"start": start.isoformat(), "end": end.isoformat(), "count": len(bars)})
        return len(bars) > 0

    span = dt.timedelta(days=probe_span_days)
    eras = _era_windows(earliest, latest, era_step_years)

    first_empty: dt.datetime | None = None
    anchor: tuple[dt.datetime, dt.datetime] | None = None
    try:
        for era_start, era_end in reversed(eras):
            if await probe(era_start, min(era_end, latest)):
                anchor = (era_start, min(era_end, latest))  # 持续更新：最终为最旧的非空档
            else:
                first_empty = era_start
                break
    except BudgetExhausted:
        return HistoryDepthResult(
            earliest_estimate=None, approx=True, granularity_days=None,
            probes=probes, calls_used=calls, complete=False,
        )

    if anchor is None:
        # 最新档也为空：整个回溯区间无数据
        return HistoryDepthResult(
            earliest_estimate=None, approx=True, granularity_days=None,
            probes=probes, calls_used=calls, complete=True,
        )
    if first_empty is None:
        # 所有档都有数据：最早可用点不晚于 earliest（已触底）
        return HistoryDepthResult(
            earliest_estimate=earliest, approx=True, granularity_days=None,
            probes=probes, calls_used=calls, complete=True,
        )

    # 二分区间：lo 已验证为空窗口，hi 初值取锚点档终点前一个窗口（连续性假设下非空）
    lo = first_empty
    hi = anchor[1] - span
    try:
        while (hi - lo) > dt.timedelta(days=1):
            mid = lo + (hi - lo) / 2
            if await probe(mid, min(mid + span, latest)):
                hi = mid
            else:
                lo = mid
        if not await probe(hi, min(hi + span, latest)):
            # 连续性假设被打破（如窗口落在长空档内）：如实报告未收敛
            return HistoryDepthResult(
                earliest_estimate=None, approx=True, granularity_days=probe_span_days,
                probes=probes, calls_used=calls, complete=True,
            )
    except BudgetExhausted:
        return HistoryDepthResult(
            earliest_estimate=hi + span, approx=True, granularity_days=probe_span_days,
            probes=probes, calls_used=calls, complete=False,
        )
    return HistoryDepthResult(
        earliest_estimate=hi + span, approx=True, granularity_days=probe_span_days,
        probes=probes, calls_used=calls, complete=True,
    )


def _era_windows(
    earliest: dt.datetime, latest: dt.datetime, era_step_years: int
) -> list[tuple[dt.datetime, dt.datetime]]:
    """从 earliest 到 latest 按 N 年一档切窗口（末档可能不满）。"""
    eras: list[tuple[dt.datetime, dt.datetime]] = []
    year = earliest.year
    while year < latest.year:
        next_year = year + era_step_years
        eras.append(
            (
                earliest.replace(year=year, month=1, day=1),
                earliest.replace(year=next_year, month=1, day=1),
            )
        )
        year = next_year
    if not eras:
        eras.append((earliest, latest))
    return eras


def window_back(end: dt.datetime, interval: Interval) -> dt.datetime:
    """按 ISO Duration 分量从 end 回溯窗口起点（年/月按日历回退，其余按时长）。"""
    year, month = end.year, end.month
    if interval.years:
        year -= interval.years
    if interval.months:
        total = year * 12 + (month - 1) - interval.months
        year, month_zero = divmod(total, 12)
        month = month_zero + 1
    day = min(end.day, calendar.monthrange(year, month)[1])
    base = end.replace(year=year, month=month, day=day)
    return base - dt.timedelta(
        weeks=interval.weeks,
        days=interval.days,
        hours=interval.hours,
        minutes=interval.minutes,
        seconds=interval.seconds,
    )
