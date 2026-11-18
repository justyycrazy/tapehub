"""探针分析算法：null 率、复权对比、历史深度、窗口回溯。

所有算法都作用于 ``WindowFetch`` 异步闭包（输入时间窗口、输出标准化 Bar 列表），
不直接依赖适配器，便于离线单测与未来 Worker 复用；调用预算由调用方控制，
预算耗尽抛出 ``BudgetExhausted``。
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Bar

# 历史深度声明的日历边界粒度 → 网格步长（月数）：month=每月 1 日、
# quarter=季初（1/4/7/10 月 1 日）、half_year=半年初（1/7 月 1 日）。
SNAP_STEPS_MONTHS: dict[str, int] = {"month": 1, "quarter": 3, "half_year": 6}
DEFAULT_SNAP = "month"


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
    # 首个出现分歧的时间戳（不一致排查的入口，None 表示一致或无重叠）
    first_divergence: dt.datetime | None = None
    # 判定所用绝对容差；max_abs_diff 始终如实记录全部差异的最大幅度
    tolerance: float = 0.0


def compare_price_series(
    a: list[Bar], b: list[Bar], field_name: str, *, tolerance: float = 0.0
) -> SeriesCompare:
    """按 timestamp 交集对比指定字段；双方任一为 null 的 bar 不参与对比。

    ``tolerance`` 为绝对容差：差异不超过容差的 bar 不判分歧（``identical``
    保持 True），用于消化跨源小数位精度等表示层差异；``max_abs_diff``
    仍如实记录全部差异的最大幅度，差异证据不因容差而丢失。
    """
    left = {bar.timestamp: getattr(bar, field_name) for bar in a}
    compared = 0
    identical = True
    max_diff = 0.0
    first_divergence: dt.datetime | None = None
    for bar in b:
        other = left.get(bar.timestamp)
        value = getattr(bar, field_name)
        if other is None or value is None:
            continue
        compared += 1
        diff = abs(other - value)
        max_diff = max(max_diff, diff)
        if diff > tolerance:
            identical = False
            if first_divergence is None:
                first_divergence = bar.timestamp
    return SeriesCompare(
        field=field_name,
        compared=compared,
        identical=identical,
        max_abs_diff=max_diff,
        first_divergence=first_divergence,
        tolerance=tolerance,
    )


@dataclass(slots=True)
class HistoryDepthResult:
    """历史深度探针结果。

    ``earliest_estimate`` 是**声明值**：首个 ≥ 首根实测的日历边界（``snap``
    粒度，保守上界口径——查询从声明起点起保证有数据，与旧「上界估计」口径
    的保证级别一致）；``first_bar`` 是首根实测（二分模式 = 中奖验证窗返回
    首根，上游截断时为返回首根；全量模式 = 精确最早 bar）；``approx=False``
    仅全量模式（首根精确且全区间覆盖）；``complete=False`` 表示预算耗尽
    提前停止。
    """

    earliest_estimate: dt.datetime | None
    first_bar: dt.datetime | None
    approx: bool
    snap: str
    probes: list[dict[str, Any]] = field(default_factory=list)
    calls_used: int = 0
    complete: bool = True


def _add_months(base: dt.datetime, months: int) -> dt.datetime:
    """从 base 推进 months 个日历月（日 clamp 到目标月末，时刻与时区保留）。"""
    total = base.year * 12 + (base.month - 1) + months
    year, month_zero = divmod(total, 12)
    month = month_zero + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


def _next_boundary(value: dt.datetime, snap: str) -> dt.datetime:
    """首个 ≥ value 的日历边界（value 恰在边界上时返回 value 本身）。

    边界取日历日零点（年/月对齐 ``snap`` 步长），时区跟随输入。
    """
    boundary = value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    step = SNAP_STEPS_MONTHS[snap]
    while boundary < value:
        boundary = _add_months(boundary, step)
    return boundary


def _grid_points(start: dt.datetime, end: dt.datetime, snap: str) -> list[dt.datetime]:
    """[start, end) 内的日历边界点（升序；start 恰为边界时含 start）。"""
    points: list[dt.datetime] = []
    boundary = _next_boundary(start, snap)
    while boundary < end:
        points.append(boundary)
        boundary = _add_months(boundary, SNAP_STEPS_MONTHS[snap])
    return points


async def run_history_depth(
    fetch: WindowFetch,
    *,
    earliest: dt.datetime,
    latest: dt.datetime,
    era_step_years: int = 5,
    max_calls: int = 24,
    snap: str = DEFAULT_SNAP,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> HistoryDepthResult:
    """纪元回溯 + 日历边界网格二分搜索最早可用窗口。

    前提假设（结果标注 approx）：数据从最早可用点起大体连续。

    1. 按 ``era_step_years`` 年一档自新向旧回溯，扫到第一个**空档**为止；
       最后一个非空档起点记为锚点；空档纪元窗口实测为空，恰好覆盖
       「锚点纪元起点之前」的全部网格窗口；
    2. 在锚点纪元内的**日历边界网格**上二分（``snap`` 粒度），谓词为
       「边界起点窗口非空」（单调性依赖连续性假设），收敛到最小非空边界
       B*——B* 之前全部边界窗逐窗实测无数据（确证无数据），B* 窗口实测
       有数据（确证有数据）→ 首根 ∈ [B*, B*+步长)；
    3. 声明值 ``earliest_estimate`` = 首个 ≥ 首根实测的日历边界（保守上界；
       首根恰落在 B* 边界上时即 B* 本身）；``first_bar`` 为 B* 验证窗返回
       首根。声明值与首根实测一并报告，回填时以声明值为准。

    ``on_step`` 为可选的逐步回调（runner 侧用于实时子步骤日志）：每轮探测
    完成后收到与 ``probes`` 记录同一条目（含 ``phase`` 阶段标签）的引用；
    本模块不做任何输出，保持纯算法层。
    """
    probes: list[dict[str, Any]] = []
    calls = 0

    async def probe(
        start: dt.datetime, end: dt.datetime, phase: str
    ) -> tuple[bool, dt.datetime | None]:
        nonlocal calls
        if calls >= max_calls:
            raise BudgetExhausted()
        calls += 1
        bars = await fetch(start, end)
        entry = {
            "phase": phase,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(bars),
        }
        probes.append(entry)
        if on_step is not None:
            on_step(entry)
        return len(bars) > 0, (bars[0].timestamp if bars else None)

    eras = _era_windows(earliest, latest, era_step_years)

    first_empty: dt.datetime | None = None
    anchor: tuple[dt.datetime, dt.datetime] | None = None
    try:
        for era_start, era_end in reversed(eras):
            if (await probe(era_start, min(era_end, latest), "era"))[0]:
                anchor = (era_start, min(era_end, latest))  # 持续更新：最终为最旧的非空档
            else:
                first_empty = era_start
                break
    except BudgetExhausted:
        return HistoryDepthResult(
            earliest_estimate=None, first_bar=None, approx=True, snap=snap,
            probes=probes, calls_used=calls, complete=False,
        )

    if anchor is None:
        # 最新档也为空：整个回溯区间无数据
        return HistoryDepthResult(
            earliest_estimate=None, first_bar=None, approx=True, snap=snap,
            probes=probes, calls_used=calls, complete=True,
        )
    if first_empty is None:
        # 所有档都有数据：最早可用点不晚于 earliest（已触底，下限不回填）
        return HistoryDepthResult(
            earliest_estimate=earliest, first_bar=None, approx=True, snap=snap,
            probes=probes, calls_used=calls, complete=True,
        )

    grid = _grid_points(anchor[0], anchor[1], snap)
    if not grid:
        # 锚点纪元内无网格点（earliest 非日历整点等配置边缘）：如实报告未收敛
        return HistoryDepthResult(
            earliest_estimate=None, first_bar=None, approx=True, snap=snap,
            probes=probes, calls_used=calls, complete=True,
        )

    step_months = SNAP_STEPS_MONTHS[snap]

    def window_end(boundary: dt.datetime) -> dt.datetime:
        return min(_add_months(boundary, step_months), latest)

    # 网格二分：lo=-1 表示 anchor_start 之前全部边界窗已被空档纪元覆盖实测为空；
    # hi 初值取网格末点（锚点纪元实测有数据，连续性假设下尾部窗口非空，未验证）
    lo = -1
    hi = len(grid) - 1
    hi_verified = False
    first_bar: dt.datetime | None = None
    try:
        while hi - lo > 1:
            mid = (lo + hi) // 2
            hit, bar_ts = await probe(grid[mid], window_end(grid[mid]), "bisect")
            if hit:
                hi = mid
                hi_verified = True
                first_bar = bar_ts
            else:
                lo = mid
        if not hi_verified:
            hit, bar_ts = await probe(grid[hi], window_end(grid[hi]), "verify")
            if not hit:
                # 连续性假设被打破（锚点纪元有数据但尾部网格窗全空）：如实报告未收敛
                return HistoryDepthResult(
                    earliest_estimate=None, first_bar=None, approx=True, snap=snap,
                    probes=probes, calls_used=calls, complete=True,
                )
            first_bar = bar_ts
    except BudgetExhausted:
        # 预算中断：已验证非空边界的声明候选照常产出（约定不回填）
        declared = _next_boundary(first_bar, snap) if first_bar is not None else None
        return HistoryDepthResult(
            earliest_estimate=declared, first_bar=first_bar, approx=True, snap=snap,
            probes=probes, calls_used=calls, complete=False,
        )

    # first_bar ∈ [B*, B*+步长) → 声明值 = B*（首根恰在边界）或 B*+步长
    return HistoryDepthResult(
        earliest_estimate=_next_boundary(first_bar, snap), first_bar=first_bar,
        approx=True, snap=snap, probes=probes, calls_used=calls, complete=True,
    )


async def run_history_depth_full(
    fetch: WindowFetch,
    *,
    earliest: dt.datetime,
    latest: dt.datetime,
    snap: str = DEFAULT_SNAP,
) -> HistoryDepthResult:
    """全量模式历史深度：单次全窗口调用直接读取最早 bar。

    适用前提（由探针文件以 ``full_history_targets`` 声明）：上游无法按日期
    过滤（如新浪期货接口每次返回全量历史，由适配器内存过滤）或全量拉取
    代价可忽略——此时纪元回溯与二分只是重复下载同一份全量数据，而单次
    调用即可得到**精确**最早时间戳（``approx=False``，``first_bar`` 为精确
    最早 bar，全区间覆盖直接证实）；「区间内无数据」的结论同样由单次全量
    覆盖直接证实。声明值与二分模式同口径：首个 ≥ 首根实测的日历边界。
    """
    probes: list[dict[str, Any]] = [
        {"phase": "full", "start": earliest.isoformat(), "end": latest.isoformat()}
    ]
    try:
        bars = await fetch(earliest, latest)
    except BudgetExhausted:
        return HistoryDepthResult(
            earliest_estimate=None, first_bar=None, approx=False, snap=snap,
            probes=probes, calls_used=0, complete=False,
        )
    probes[0]["count"] = len(bars)
    first_bar = bars[0].timestamp if bars else None
    return HistoryDepthResult(
        earliest_estimate=_next_boundary(first_bar, snap) if first_bar else None,
        first_bar=first_bar,
        approx=False,
        snap=snap,
        probes=probes,
        calls_used=1,
        complete=True,
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


def history_declaration_compat(
    declared: dt.datetime | None, first_bar: dt.datetime | None
) -> str:
    """history.start 声明相容性月粒度方向判定（2026-11-18 裁决：只看
    first_bar 实测首根，earliest_estimate 二分估计不参与判定）。

    history.start 的承诺语义是「从声明日起可查」；统一口径为月首对齐，
    故按日历月比对与口径语义自洽：

    - 相同月 → ``compatible``（月首对齐语义下正确）
    - 首根月 > 声明月 → ``over_claim``（声明承诺区间内实测无数据——
      走「复探差异处置」：报告并等待人工确认是否回填）
    - 首根月 < 声明月 → ``under_claim``（保守成立；差值大时为精化候选
      信息性展示）
    - 任一输入缺失 → ``unknown``（声明 null 未核实 / 未测出首根，不判定）
    """
    if declared is None or first_bar is None:
        return "unknown"
    declared_month = (declared.year, declared.month)
    first_bar_month = (first_bar.year, first_bar.month)
    if first_bar_month == declared_month:
        return "compatible"
    if first_bar_month > declared_month:
        return "over_claim"
    return "under_claim"
