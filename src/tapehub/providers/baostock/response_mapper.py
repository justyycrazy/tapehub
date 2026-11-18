"""baostock 响应映射（ResponseMapper）：上游字符串行 → 统一 Bar 模型。

列名/单位/时间戳口径见 docs/providers/baostock.md（唯一出处，本文不复述）。
官方文档：https://baostock.com/mainContent?file=pythonAPI.md。

上游行值全为字符串，缺失值统一为空串 ``""``，转换时映射为 null；
Provider 特有字段仅经受控白名单进入 extensions。
"""

import math
from datetime import datetime
from typing import Any

from tapehub.domain.errors import UpstreamError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import CN_TIMEZONE, AssetType, Bar

# 分钟族周期集合（时间戳走 date + time 解析，无 extensions）
_MINUTE_INTERVALS = frozenset(
    Interval.parse(code) for code in ("PT5M", "PT15M", "PT30M", "PT1H")
)

# 上游列名 → 标准字段名（OHLC + 量额为三套 fields 公共部分）
_COLUMNS = {
    "date": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
}

# 个股日线：涨跌幅/换手率/交易状态/ST 标记 + 估值四项（官方算法见文档）
_STOCK_DAILY_EXTENSIONS = {
    "preclose": "prev_close",
    "turn": "turnover_rate",
    "pctChg": "pct_change",
    "tradestatus": "trade_status",
    "isST": "is_st",
    "peTTM": "pe_ttm",
    "pbMRQ": "pb_mrq",
    "psTTM": "ps_ttm",
    "pcfNcfTTM": "pcf_ncf_ttm",
}
# 指数日线：无交易状态/ST/估值字段
_INDEX_DAILY_EXTENSIONS = {"preclose": "prev_close", "pctChg": "pct_change"}
# 周月线：无 preclose/tradestatus/isST/估值
_WEEKLY_MONTHLY_EXTENSIONS = {"turn": "turnover_rate", "pctChg": "pct_change"}


def _to_float(value: Any) -> float | None:
    """转换为 float；None/空串/NaN/±inf/不可转换值返回 None，不用 0 冒充。

    ±inf 与 NaN 同语义置 null：JSON 路由经 pydantic 序列化会把 inf 静默
    置 null（未声明口径），SSE 帧经 ``json.dumps`` 则输出非法 JSON token
    （``Infinity``，严格解析器拒收）——非有限值不进入统一口径。
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _daily_timestamp(row: dict[str, Any]) -> datetime:
    """交易日转换为该日 00:00 北京时间（+08:00）。"""
    text = str(row.get("date") or "")
    if len(text) < 10:
        raise UpstreamError(
            "baostock row is missing a valid date field.",
            details={"field": "date", "value": row.get("date")},
        )
    return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=CN_TIMEZONE)


def _minute_timestamp(row: dict[str, Any]) -> datetime:
    """date + time（YYYYMMDDHHMMSSsss）解析为北京时间。

    time 为交易所行情时间（17 位数字：YYYYMMDDHHMMSSsss），缺失按契约
    违背处理——分钟线时间戳是唯一时间语义，不做推断。
    """
    time_text = str(row.get("time") or "")
    if len(time_text) < 14:
        raise UpstreamError(
            "baostock minute row is missing a valid time field.",
            details={"field": "time", "value": row.get("time")},
        )
    return datetime(
        year=int(time_text[0:4]),
        month=int(time_text[4:6]),
        day=int(time_text[6:8]),
        hour=int(time_text[8:10]),
        minute=int(time_text[10:12]),
        second=int(time_text[12:14]),
        microsecond=int(time_text[14:17].ljust(6, "0")),
        tzinfo=CN_TIMEZONE,
    )


def _extensions(row: dict[str, Any], whitelist: dict[str, str]) -> dict[str, Any] | None:
    """白名单内的上游字段 → extensions；值为空（转换后 None）不写入。"""
    bar_extensions: dict[str, Any] = {}
    for source, name in whitelist.items():
        value = _to_float(row.get(source))
        if value is not None:
            bar_extensions[name] = value
    return bar_extensions or None


def _extensions_for(asset_type: AssetType, interval: Interval) -> dict[str, str]:
    """按 (资产类型, 周期族) 选择 extensions 白名单。"""
    if interval in _MINUTE_INTERVALS:
        return {}
    if interval == Interval.parse("P1D"):
        if asset_type == AssetType.STOCK:
            return _STOCK_DAILY_EXTENSIONS
        return _INDEX_DAILY_EXTENSIONS
    return _WEEKLY_MONTHLY_EXTENSIONS


def to_bar(row: dict[str, Any], *, asset_type: AssetType, interval: Interval) -> Bar:
    """单行上游数据（按 fields 名成键）→ 标准 Bar。值为空串时标准字段为 null。"""
    if interval in _MINUTE_INTERVALS:
        timestamp = _minute_timestamp(row)
    else:
        timestamp = _daily_timestamp(row)
    values = {field: _to_float(row.get(source)) for source, field in _COLUMNS.items()}
    return Bar(
        timestamp=timestamp,
        open=values.get("open"),
        high=values.get("high"),
        low=values.get("low"),
        close=values.get("close"),
        volume=values.get("volume"),
        amount=values.get("amount"),
        extensions=_extensions(row, _extensions_for(asset_type, interval)),
    )


def to_standard_bars(
    asset_type: AssetType, interval: Interval, rows: list[dict[str, Any]]
) -> list[Bar]:
    """按 (资产类型, 周期族) 选择映射配置，批量转换为标准 Bar 列表。

    上游函数单一（query_history_k_data_plus），映射配置按查询维度而非
    operation 字符串区分。
    """
    return [to_bar(row, asset_type=asset_type, interval=interval) for row in rows]
