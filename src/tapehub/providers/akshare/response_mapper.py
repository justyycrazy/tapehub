"""akshare 响应映射（ResponseMapper）：上游 DataFrame 行 → 统一 Bar 模型。

列名/单位/换算口径见 docs/providers/akshare.md（唯一出处，本文不复述）。

缺失值与 NaN 统一映射为 null；Provider 特有字段仅通过受控白名单进入 extensions。
"""

import math
from datetime import date as date_type
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from tapehub.domain.models import CN_TIMEZONE, Bar

# 美股 datetime 的显示时区（ZoneInfo 自动处理 EST/EDT 夏令时切换；
# 命名对齐 twelvedata 的同口径实现）
NEW_YORK_TIMEZONE = ZoneInfo("America/New_York")

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
    """转换为 float；None/NaN/±inf/不可转换值返回 None，不用 0 冒充。

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


def _to_timestamp(value: Any, tz: ZoneInfo = CN_TIMEZONE) -> datetime:
    """交易日转换为该日 00:00 的指定时区感知时间（默认北京时间）。"""
    if isinstance(value, datetime):
        naive = value
    elif isinstance(value, date_type):
        naive = datetime(value.year, value.month, value.day)
    else:
        text = str(value)[:10]
        naive = datetime.strptime(text, "%Y-%m-%d")
    return datetime(naive.year, naive.month, naive.day, tzinfo=tz)


def _timestamp_source(columns: dict[str, str]) -> str:
    return next(source for source, field in columns.items() if field == "timestamp")


def to_bar(
    row: dict[str, Any],
    *,
    columns: dict[str, str],
    extensions: dict[str, str] | None = None,
    volume_scale: float | None = None,
    timestamp_tz: ZoneInfo = CN_TIMEZONE,
) -> Bar:
    """单行上游数据 → 标准 Bar。列缺失或值为空时标准字段为 null。"""
    timestamp = _to_timestamp(row.get(_timestamp_source(columns)), timestamp_tz)
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
    # 东财 ETF 与个股同端点同列（ETF 无「股票代码」列，dict 按列名取值天然兼容）
    "fund_etf_hist_em": {
        "columns": _STOCK_EM_COLUMNS,
        "extensions": _STOCK_EM_EXTENSIONS,
        "volume_scale": _STOCK_EM_VOLUME_SCALE,
    },
    # 东财指数与个股同端点同列；成交量单位手、原样透出（无 volume_scale，
    # 与新浪/腾讯指数口径一致）
    "index_zh_a_hist": {
        "columns": _STOCK_EM_COLUMNS,
        "extensions": _STOCK_EM_EXTENSIONS,
    },
    # 港股/美股与 A 股同列；成交量单位未核实，无 volume_scale（原样透出，
    # 依据见 docs/providers/akshare.md）；美股日线为美东零点口径（BarBatch
    # 统一归一化北京时间）
    "stock_hk_hist": {
        "columns": _STOCK_EM_COLUMNS,
        "extensions": _STOCK_EM_EXTENSIONS,
    },
    "stock_us_hist": {
        "columns": _STOCK_EM_COLUMNS,
        "extensions": _STOCK_EM_EXTENSIONS,
        "timestamp_tz": NEW_YORK_TIMEZONE,
    },
    "stock_zh_a_daily": {
        "columns": _STOCK_SINA_COLUMNS,
        "extensions": _STOCK_SINA_EXTENSIONS,
    },
    "stock_zh_a_hist_tx": {
        "columns": _STOCK_TX_COLUMNS,
        "extensions": _STOCK_TX_EXTENSIONS,
    },
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
