"""同花顺（ths）响应映射（ResponseMapper）：ApiResponse 信封数据 → 统一 Bar / Quote 模型。

信封解析（``code``/``data`` 拆包）在 adapter 完成，本模块消费 ``data`` 容器：

bars（``data.item``，两种行形态统一映射，字段名官方文档核实 2026-11-13）：

- A 股/指数/ETF 历史 K 线：``date_ms``/``open_price``/``high_price``/
  ``low_price``/``close_price``/``volume``/``turnover``；
- 期货/期权日 K：``timestamp``/``open_price``/``high_price``/``low_price``/
  ``close_price``/``volume``/``turnover``（时间键名不同，其余同构）；
- ``turnover`` → 标准 ``amount``；缺失/NaN → null（不用 0 冒充）；
  行缺失时间键（``date_ms``/``timestamp`` 均无）属上游契约违背（bar 无时间戳
  无法标准化），抛 UPSTREAM_ERROR——上游对单字段标明可空，但时间戳是 bar
  的身份键，loud 失败优于静默丢行；
- 单位：A 股档 volume=股、turnover=元（官方文档明示）；ETF/期货/期权
  volume/turnover 单位上游未明示，原值透出（不编造换算）。

quote（``data`` 容器，按 operation 显式分派——形态分派会把畸形快照 item
误路由到分时路径，报错语义失真）：

- 快照（a_share_snapshot/index_snapshot/fund_snapshot，item[] 含
  ``last_price``）：``last_price`` 直映、``volume``/``turnover`` →
  ``volume``/``amount``；``data.timestamp`` 为本次快照最新上游有效时间
  （null 视为无有效数据，契约违背）；extensions 白名单：open_price/
  high_price/low_price/prev_price/price_change/price_change_ratio_pct/
  price_amplitude_ratio_pct/turnover_ratio_pct（快照不返回中文名 name，
  不透出）；
- 分时（futures_snapshot/options_snapshot，item[] 含 ``price``）：防御按
  点时间升序排后取末点——``last_price``=末点 ``price``、``timestamp``=
  末点 ``timestamp``（行情时间）；**volume/amount=全点求和（当日累计）**
  （2026-11-13 探针实证分时点量额为逐分钟增量：各点 turnover/volume
  恰等于该点 price×合约乘数、开盘三点 volume 5→26→31 非累计序列，报告
  20261113T212034+0800——累计口径需全点求和，volume/amount 两字段独立
  计算（任一点 volume 缺失则 volume 恒 null、任一点 turnover 缺失则
  amount 恒 null，部分和不冒充累计））；``item[].timestamp``
  官方标明可空（盘前等场景），无时间戳的点不参与排序，全部无时间戳
  或 item 空属无有效行情，抛 UPSTREAM_ERROR；
- 时间戳统一毫秒 Unix → 北京时间 aware datetime（经 timeutil 纪元算术，
  Windows 安全）；快照 ``data.timestamp`` 实测为上游响应组装时刻
  （盘后随墙钟推进、间隔 10s 两次调用精确 +10s，非成交时点）——上游
  快照无独立行情时间字段，按文档「最新上游有效时间」口径透出。
"""

import math
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UpstreamError
from tapehub.domain.models import CN_TIMEZONE, Bar, Quote
from tapehub.domain.timeutil import epoch_to_datetime

# 快照 extensions 白名单（官方文档核实字段；缺失不透出）
_QUOTE_EXTENSIONS_FIELDS = (
    "open_price",
    "high_price",
    "low_price",
    "prev_price",
    "price_change",
    "price_change_ratio_pct",
    "price_amplitude_ratio_pct",
    "turnover_ratio_pct",
)

# quotes 路径分派（与 request_mapper 的 operation 命名一致；未知 operation
# 属内部编程错误，防御性拒绝）
_SNAPSHOT_OPERATIONS = frozenset({"a_share_snapshot", "index_snapshot", "fund_snapshot"})
_INTRADAY_OPERATIONS = frozenset({"futures_snapshot", "options_snapshot"})


def _to_float(value: Any) -> float | None:
    """转换为 float；None/NaN/不可转换值返回 None，不用 0 冒充。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result):
        return None
    return result


def _ms_to_datetime(value: Any, field: str) -> datetime:
    """毫秒 Unix 时间戳 → 北京时间 aware datetime；缺失/非法属契约违背。

    经 timeutil 纪元算术（不用 ``datetime.fromtimestamp``，Windows 对早期
    时间戳受限）。``field`` 供错误 details 定位（date_ms/timestamp）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpstreamError(
            "ths response has no valid timestamp.",
            details={"field": field},
        )
    return epoch_to_datetime(int(value) * 1_000_000, CN_TIMEZONE)


def _data_items(data: Any, *, what: str) -> list[dict[str, Any]]:
    """信封 ``data.item`` 防御提取：非列表或过滤后无对象行属上游契约违背
    或无有效数据（上游合法无数据返回 ``item=[]``，同样按无数据拒绝）。"""
    if not isinstance(data, dict):
        raise UpstreamError(
            "ths response data is not a JSON object.",
            details={"provider": "ths"},
        )
    items = data.get("item")
    if not isinstance(items, list):
        raise UpstreamError(
            f"ths response has no {what}.",
            details={"provider": "ths"},
        )
    rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        raise UpstreamError(
            f"ths response has no {what}.",
            details={"provider": "ths"},
        )
    return rows


def to_standard_bars(rows: list[dict[str, Any]]) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一升序排序）。

    A 股/指数/ETF（``date_ms``）与期货/期权日 K（``timestamp``）同构映射，
    时间键防御兼容两种形态。
    """
    bars: list[Bar] = []
    for row in rows:
        raw_ts = row.get("date_ms")
        if raw_ts is None:
            raw_ts = row.get("timestamp")
        bars.append(
            Bar(
                timestamp=_ms_to_datetime(raw_ts, "date_ms"),
                open=_to_float(row.get("open_price")),
                high=_to_float(row.get("high_price")),
                low=_to_float(row.get("low_price")),
                close=_to_float(row.get("close_price")),
                volume=_to_float(row.get("volume")),
                amount=_to_float(row.get("turnover")),
            )
        )
    return bars


def to_standard_quote(
    data: Any, operation: str, *, provider: str, received_at: datetime
) -> Quote:
    """信封 ``data`` → 标准 Quote（按 operation 显式分派，口径见模块 docstring）。

    - 快照：``data.timestamp``（本次快照最新上游有效时间）为行情时间；
    - 分时：末点时间为行情时间（``data.timestamp`` 为数据时间，不采用）。
    """
    if not isinstance(data, dict):
        raise UpstreamError(
            "ths response data is not a JSON object.",
            details={"provider": provider},
        )
    items = _data_items(data, what="quote item")
    if operation in _SNAPSHOT_OPERATIONS:
        return _snapshot_to_quote(data, items, provider=provider, received_at=received_at)
    if operation in _INTRADAY_OPERATIONS:
        return _intraday_to_quote(items, provider=provider, received_at=received_at)
    raise UpstreamError(
        "ths quote mapper has no path for operation.",
        details={"provider": provider, "operation": operation},
    )


def _snapshot_to_quote(
    data: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    provider: str,
    received_at: datetime,
) -> Quote:
    """快照形（A 股/指数/ETF）：单标的请求防御取首个匹配 item。"""
    timestamp = _ms_to_datetime(data.get("timestamp"), "timestamp")
    item = items[0]
    last_price = _to_float(item.get("last_price"))
    if last_price is None:
        raise UpstreamError(
            "ths quote payload has no valid last_price.",
            details={"provider": provider, "field": "last_price"},
        )
    extensions: dict[str, Any] = {}
    for field in _QUOTE_EXTENSIONS_FIELDS:
        value = _to_float(item.get(field))
        if value is not None:
            extensions[field] = value
    return Quote(
        timestamp=timestamp,
        last_price=last_price,
        volume=_to_float(item.get("volume")),
        amount=_to_float(item.get("turnover")),
        provider=provider,
        received_at=received_at,
        extensions=extensions or None,
    )


def _intraday_to_quote(
    items: list[dict[str, Any]],
    *,
    provider: str,
    received_at: datetime,
) -> Quote:
    """分时形（期货/期权）：升序排后取末点作 last_price（口径见模块 docstring）。

    volume/amount 为全点求和（当日累计，探针实证分时点为逐分钟增量），
    两字段独立计算：任一点 volume 缺失则 volume 恒 null、任一点 turnover
    缺失则 amount 恒 null（部分和不冒充累计）；null 时间戳点被跳过导致
    序列不完整时两字段均不计算。
    """
    points: list[tuple[datetime, dict[str, Any]]] = []
    skipped_no_timestamp = 0
    for item in items:
        raw_ts = item.get("timestamp")
        if raw_ts is None:  # 官方标明可空（盘前等场景），不参与排序
            skipped_no_timestamp += 1
            continue
        points.append((_ms_to_datetime(raw_ts, "timestamp"), item))
    if not points:
        raise UpstreamError(
            "ths intraday series has no valid timestamp.",
            details={"provider": provider, "field": "timestamp"},
        )
    points.sort(key=lambda pair: pair[0])
    last_time, last_point = points[-1]
    last_price = _to_float(last_point.get("price"))
    if last_price is None:
        raise UpstreamError(
            "ths intraday last point has no valid price.",
            details={"provider": provider, "field": "price"},
        )
    # 有跳点时求和序列不完整（跳过点的量额不可见），累计口径不成立
    volume: float | None = None
    amount: float | None = None
    if not skipped_no_timestamp:
        volume = _sum_series([_to_float(p.get("volume")) for _, p in points])
        amount = _sum_series([_to_float(p.get("turnover")) for _, p in points])
    return Quote(
        timestamp=last_time,
        last_price=last_price,
        volume=volume,
        amount=amount,
        provider=provider,
        received_at=received_at,
        extensions=None,
    )


def _sum_series(values: list[float | None]) -> float | None:
    """分时点单字段序列求和（当日累计）；任一点缺失（null）即返回 null——
    部分和不冒充累计。volume/amount 两字段独立调用，互不牵连。"""
    if not values or any(value is None for value in values):
        return None
    return float(sum(values))
