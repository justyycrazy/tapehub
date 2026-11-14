"""富途（futunn）响应映射（ResponseMapper）：信封业务数据 → 统一 Bar / Quote 模型。

信封解析（``ret_code``/``data`` 拆包）在 adapter 完成，本模块消费业务容器：

bars（``data.kline_list``，行字段官方文档核实 2026-11-14）：

- 行字段 ``time_key``（毫秒 Unix 时间戳，绝对时刻）/``open``/``close``/
  ``high``/``low``/``volume``/``turnover``；
- ``data.volume_precision``：行内 ``volume`` 已被放大 10^n 倍需自行还原
  （官方口径：仅事件合约/数字货币等特殊品类可能 >0，股票一般 n=0）——
  缺失/非法按 0 处理（不缩放，即官方默认口径）；
- ``time_key`` 为绝对时刻，统一经纪元算术转北京时间（Windows 安全，不用
  ``datetime.fromtimestamp``）；行内 ``time_zone`` 为显示用本地时区偏移
  （相对 UTC 分钟数），不参与标准化——绝对时刻不依赖时区标注；
- ``turnover`` → 标准 ``amount``；缺失/NaN → null（不用 0 冒充）；行缺失
  ``time_key`` 属上游契约违背（bar 无时间戳无法标准化），抛 UPSTREAM_ERROR
  ——时间戳是 bar 的身份键，loud 失败优于静默丢行；
- 合法无数据上游返回 ``kline_list=[]``（如停牌/窗口越界），映射为空批次；
- 单位：volume=股、turnover=成交额（各市场原币），原值透出（官方未标注
  币种折算，不编造换算）。

quote（``data.quote_list``，单标的取匹配 code 行，由 adapter 完成）：

- ``data_time``（毫秒 Unix）→ 行情时间（0/None 官方语义「不适用品类」，
  视为无有效行情时间，契约违背）；``last_price`` 直映（缺失属无有效
  行情，契约违背）；``volume``/``turnover`` → ``volume``/``amount``；
- 上游轻量快照无盘口五档：bid/ask 恒 null（如实透出，不编造）；
- extensions 白名单：``open_price``/``high_price``/``low_price``/
  ``prev_close_price``/``turnover_rate``/``amplitude``（% 数值原样透出，
  缺失不透出）+ ``suspension``（布尔，停牌标记）；中文名 name 不透出；
- 上游对 code_list 中非法 code 静默丢弃（ret_code 仍 0）：匹配行缺失
  属无有效行情，抛 UPSTREAM_ERROR（adapter 完成 code 匹配）。
"""

import math
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UpstreamError
from tapehub.domain.models import CN_TIMEZONE, Bar, Quote
from tapehub.domain.timeutil import epoch_to_datetime

# 快照 extensions 白名单（官方文档核实字段；缺失不透出；% 数值原样透出）
_QUOTE_EXTENSIONS_FIELDS = (
    "open_price",
    "high_price",
    "low_price",
    "prev_close_price",
    "turnover_rate",
    "amplitude",
    "suspension",
)


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
    """毫秒 Unix 时间戳 → 北京时间 aware datetime（绝对时刻）。

    经 timeutil 纪元算术（不用 ``datetime.fromtimestamp``，Windows 对早期
    时间戳受限）。``field`` 供错误 details 定位（time_key/data_time）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpstreamError(
            "futunn response has no valid timestamp.",
            details={"provider": "futunn", "field": field},
        )
    return epoch_to_datetime(int(value) * 1_000_000, CN_TIMEZONE)


def to_standard_bars(
    rows: list[dict[str, Any]], *, volume_precision: int
) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一升序排序）。

    ``volume_precision`` 来自信封 ``data.volume_precision``（整批一致），
    行内 volume 已放大 10^n 倍，逐行除以 10^n 还原真实成交量。
    """
    divisor = 10**volume_precision
    bars: list[Bar] = []
    for row in rows:
        volume = _to_float(row.get("volume"))
        if volume is not None and divisor != 1:
            volume = volume / divisor
        bars.append(
            Bar(
                timestamp=_ms_to_datetime(row.get("time_key"), "time_key"),
                open=_to_float(row.get("open")),
                high=_to_float(row.get("high")),
                low=_to_float(row.get("low")),
                close=_to_float(row.get("close")),
                volume=volume,
                amount=_to_float(row.get("turnover")),
            )
        )
    return bars


def to_standard_quote(
    row: dict[str, Any], *, provider: str, received_at: datetime
) -> Quote:
    """快照行 → 标准 Quote（上游无盘口五档，bid/ask 恒 null）。"""
    raw_ts = row.get("data_time")
    if (
        isinstance(raw_ts, bool)
        or not isinstance(raw_ts, (int, float))
        or int(raw_ts) <= 0
    ):
        # 0/None 为官方「不适用品类」语义：无有效行情时间，契约违背拒绝
        raise UpstreamError(
            "futunn quote payload has no valid data_time.",
            details={"provider": provider, "field": "data_time"},
        )
    last_price = _to_float(row.get("last_price"))
    if last_price is None:
        raise UpstreamError(
            "futunn quote payload has no valid last_price.",
            details={"provider": provider, "field": "last_price"},
        )
    extensions: dict[str, Any] = {}
    for field in _QUOTE_EXTENSIONS_FIELDS:
        if field == "suspension":
            if isinstance(row.get(field), bool):
                extensions[field] = row[field]
            continue
        value = _to_float(row.get(field))
        if value is not None:
            extensions[field] = value
    return Quote(
        timestamp=_ms_to_datetime(raw_ts, "data_time"),
        last_price=last_price,
        volume=_to_float(row.get("volume")),
        amount=_to_float(row.get("turnover")),
        provider=provider,
        received_at=received_at,
        extensions=extensions or None,
    )


__all__ = ["to_standard_bars", "to_standard_quote"]
