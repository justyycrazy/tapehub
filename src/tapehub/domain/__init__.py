"""Domain 层：统一领域模型、周期规范、能力、错误与端口。

本层不依赖 FastAPI、PyYAML、Provider SDK 或具体基础设施。
"""

from tapehub.domain.capabilities import Capability, IntervalEntry, IntervalHistory
from tapehub.domain.errors import ErrorCode, TapehubError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import (
    Adjust,
    AssetType,
    Bar,
    BarBatch,
    BarQuery,
    DataType,
    Delivery,
    Instrument,
)
from tapehub.domain.ports import HistoricalBarProvider

__all__ = [
    "Adjust",
    "AssetType",
    "Bar",
    "BarBatch",
    "BarQuery",
    "Capability",
    "DataType",
    "Delivery",
    "ErrorCode",
    "HistoricalBarProvider",
    "Instrument",
    "Interval",
    "IntervalEntry",
    "IntervalHistory",
    "TapehubError",
]
