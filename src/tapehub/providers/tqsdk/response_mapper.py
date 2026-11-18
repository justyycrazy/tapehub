"""TqSdk 响应映射（ResponseMapper）：上游数据 → 统一模型。

字段结构与快照白名单口径见 docs/providers/tqsdk.md（唯一出处，本文不复述）。

K 线 datetime 为自 unix epoch 以来的纳秒数，规范为北京时间（+08:00）；
无成交额字段：标准 ``amount`` 恒为 null；缺失值与 NaN 统一映射为 null
（量侧真实 0 保留 0.0，不用 null 冒充）。快照 datetime 为北京时间墙钟
字符串；指数与加权指数无盘口：bid/ask 价格为 NaN → null 如实透出。
"""

import math
import time
from datetime import datetime
from typing import Any

from tapehub.domain.models import CN_TIMEZONE, Bar, Quote
from tapehub.domain.timeutil import epoch_to_datetime

_EXTENSIONS_FIELDS = ("open_oi", "close_oi")

# 快照 extensions 白名单（未核实字段不透出，依据见 docs/providers/tqsdk.md）
_QUOTE_EXTENSIONS_FIELDS = (
    "open",
    "highest",
    "lowest",
    "average",
    "pre_settlement",
    "pre_close",
    "open_interest",
    "pre_open_interest",
    "upper_limit",
    "lower_limit",
)


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


def _to_timestamp_ns(value: Any) -> datetime:
    """纳秒时间戳 → 北京时间 datetime（+08:00）（纪元算术，见 domain.timeutil）。"""
    return epoch_to_datetime(int(value), CN_TIMEZONE)


def _to_quote_datetime(value: Any) -> datetime:
    """quote datetime（北京时间墙钟字符串）→ 北京时间 datetime（+08:00）。

    首包未到达时上游为空串/NaN——调用方（Runtime）保证只在首包后发布快照，
    此处遇空值显式抛错（由应用层包装为 UPSTREAM_ERROR），不用假时间冒充。
    """
    text = "" if value is None else str(value).strip()
    if not text or text.lower() == "nan":
        raise ValueError(f"tqsdk quote datetime is empty or invalid: {value!r}")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:  # 防御：意外带偏移时归一化
        return parsed.astimezone(CN_TIMEZONE)
    return parsed.replace(tzinfo=CN_TIMEZONE)


def to_standard_bars(rows: list[dict[str, Any]]) -> list[Bar]:
    """上游行列表 → 标准 Bar 列表（保持上游顺序，由分页组件统一排序）。"""
    bars: list[Bar] = []
    for row in rows:
        extensions: dict[str, Any] = {}
        for field in _EXTENSIONS_FIELDS:
            value = _to_float(row.get(field))
            if value is not None:
                extensions[field] = value
        bars.append(
            Bar(
                timestamp=_to_timestamp_ns(row.get("datetime")),
                open=_to_float(row.get("open")),
                high=_to_float(row.get("high")),
                low=_to_float(row.get("low")),
                close=_to_float(row.get("close")),
                volume=_to_float(row.get("volume")),  # 期货成交量保持手
                amount=None,  # 上游无成交额字段
                extensions=extensions or None,
            )
        )
    return bars


def to_standard_quote(
    snapshot: dict[str, Any], *, provider: str, received_at: datetime
) -> Quote:
    """上游快照 dict（Runtime 已拷贝的纯 dict）→ 标准 Quote 模型。

    五档价格/量成对映射：价格 NaN → null（指数无盘口）、量侧真实 0 保留 0.0。
    ``provider``/``received_at`` 由适配器传入（同 BarBatch 的装配语义）。
    """
    book: dict[str, float | None] = {}
    for level in range(1, 6):
        for side in ("bid", "ask"):
            book[f"{side}_price{level}"] = _to_float(snapshot.get(f"{side}_price{level}"))
            book[f"{side}_volume{level}"] = _to_float(snapshot.get(f"{side}_volume{level}"))
    extensions: dict[str, Any] = {}
    for field in _QUOTE_EXTENSIONS_FIELDS:
        value = _to_float(snapshot.get(field))
        if value is not None:
            extensions[field] = value
    # 快照新鲜度：Runtime 打的到达时刻（`_arrival`，墙钟秒）→ 距今毫秒。
    # 断连/静默窗口内命中缓存的快照可据此判陈旧，绝不冒充新鲜；无戳
    # （per_request 独立实例路径等）不透出
    arrival = snapshot.get("_arrival")
    if isinstance(arrival, (int, float)) and not isinstance(arrival, bool):
        extensions["snapshot_age_ms"] = max(0, round((time.time() - arrival) * 1000))
    return Quote(
        timestamp=_to_quote_datetime(snapshot.get("datetime")),
        last_price=_to_float(snapshot.get("last_price")),
        volume=_to_float(snapshot.get("volume")),
        amount=_to_float(snapshot.get("amount")),
        provider=provider,
        received_at=received_at,
        extensions=extensions or None,
        **book,  # type: ignore[arg-type]
    )
