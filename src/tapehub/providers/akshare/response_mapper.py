"""akshare 响应映射（ResponseMapper）：上游 DataFrame 行 → 统一 Bar 模型。

列名与单位均已按官方文档核实：

- stock_zh_a_hist（东财个股）：中文列名，成交量单位为**手**（标准化为股，×100）；
- stock_zh_a_daily（新浪个股）：英文列名，成交量单位为股；
- stock_zh_a_hist_tx（腾讯个股）：英文列名，成交量单位为股；
- stock_zh_index_daily_em：英文列名；
- stock_zh_index_daily（新浪指数）：无成交额列；
- stock_zh_index_daily_tx：无独立成交额列，上游 amount 列语义为成交量（单位手）；
- futures_zh_daily_sina：无日期参数接口，含 hold（持仓量）、settle（结算价）。

缺失值与 NaN 统一映射为 null；Provider 特有字段仅通过受控白名单进入 extensions。
"""

import math
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Any

from tapehub.domain.models import Bar

# 列名 → 标准字段名
_STOCK_EM_COLUMNS = {
    "日期": "timestamp",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}
# 东财个股成交量单位为手，标准化为股
_STOCK_EM_VOLUME_SCALE = 100.0
_STOCK_EM_EXTENSIONS = {
    "振幅": "amplitude",
    "涨跌幅": "pct_change",
    "涨跌额": "change",
    "换手率": "turnover_rate",
}

_STOCK_SINA_COLUMNS = {
    "date": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
}
_STOCK_SINA_EXTENSIONS = {"outstanding_share": "outstanding_share", "turnover": "turnover"}

_STOCK_TX_COLUMNS = dict(_STOCK_SINA_COLUMNS)
_STOCK_TX_EXTENSIONS = {"turnover": "turnover"}

_INDEX_EM_COLUMNS = dict(_STOCK_SINA_COLUMNS)
_INDEX_EM_EXTENSIONS: dict[str, str] = {}

_INDEX_SINA_COLUMNS = {
    "date": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}

# 腾讯指数：上游 amount 列语义为成交量（单位手），映射到标准 volume
_INDEX_TX_COLUMNS = {
    "date": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "amount": "volume",
}

_FUTURES_SINA_COLUMNS = _INDEX_SINA_COLUMNS
_FUTURES_SINA_EXTENSIONS = {"hold": "hold", "settle": "settle"}


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


def _to_timestamp(value: Any) -> datetime:
    """交易日转换为该日 00:00 UTC。"""
    if isinstance(value, datetime):
        naive = value
    elif isinstance(value, date_type):
        naive = datetime(value.year, value.month, value.day)
    else:
        text = str(value)[:10]
        naive = datetime.strptime(text, "%Y-%m-%d")
    return datetime(naive.year, naive.month, naive.day, tzinfo=timezone.utc)


def _timestamp_source(columns: dict[str, str]) -> str:
    return next(source for source, field in columns.items() if field == "timestamp")


def to_bar(
    row: dict[str, Any],
    *,
    columns: dict[str, str],
    extensions: dict[str, str] | None = None,
    volume_scale: float | None = None,
) -> Bar:
    """单行上游数据 → 标准 Bar。列缺失或值为空时标准字段为 null。"""
    timestamp = _to_timestamp(row.get(_timestamp_source(columns)))
    values: dict[str, Any] = {}
    for source, field in columns.items():
        if field == "timestamp":
            continue
        values[field] = _to_float(row.get(source))
    if volume_scale is not None and values.get("volume") is not None:
        values["volume"] = values["volume"] * volume_scale
    bar_extensions: dict[str, Any] = {}
    for source, name in (extensions or {}).items():
        value = _to_float(row.get(source))
        if value is not None:
            bar_extensions[name] = value
    return Bar(
        timestamp=timestamp,
        open=values.get("open"),
        high=values.get("high"),
        low=values.get("low"),
        close=values.get("close"),
        volume=values.get("volume"),
        amount=values.get("amount"),
        extensions=bar_extensions or None,
    )


# operation → 响应映射配置
_MAPPINGS: dict[str, dict[str, Any]] = {
    "stock_zh_a_hist": {
        "columns": _STOCK_EM_COLUMNS,
        "extensions": _STOCK_EM_EXTENSIONS,
        "volume_scale": _STOCK_EM_VOLUME_SCALE,
    },
    "stock_zh_a_daily": {
        "columns": _STOCK_SINA_COLUMNS,
        "extensions": _STOCK_SINA_EXTENSIONS,
    },
    "stock_zh_a_hist_tx": {
        "columns": _STOCK_TX_COLUMNS,
        "extensions": _STOCK_TX_EXTENSIONS,
    },
    "stock_zh_index_daily_em": {"columns": _INDEX_EM_COLUMNS, "extensions": _INDEX_EM_EXTENSIONS},
    "stock_zh_index_daily": {"columns": _INDEX_SINA_COLUMNS},
    "stock_zh_index_daily_tx": {"columns": _INDEX_TX_COLUMNS},
    "futures_zh_daily_sina": {
        "columns": _FUTURES_SINA_COLUMNS,
        "extensions": _FUTURES_SINA_EXTENSIONS,
    },
}


def to_standard_bars(operation: str, rows: list[dict[str, Any]]) -> list[Bar]:
    """按 operation 选择映射配置，批量转换为标准 Bar 列表。"""
    config = _MAPPINGS.get(operation)
    if config is None:
        raise ValueError(f"no response mapping for operation {operation!r}")
    return [to_bar(row, **config) for row in rows]
