"""Provider 能力模型。

能力目录是路由与能力查询接口共用的唯一事实来源，
避免“能力接口显示支持，但实际路由不支持”的不一致。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, DataType, Delivery, ensure_utc


class IntervalHistory(BaseModel):
    """已知的历史数据覆盖范围；无法确定时保持 null，不得编造。"""

    model_config = ConfigDict(frozen=True)

    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value, "history boundary")


class IntervalEntry(BaseModel):
    """能力清单中的时间周期项。"""

    model_config = ConfigDict(frozen=True)

    code: Interval
    history: IntervalHistory = IntervalHistory()


class Capability(BaseModel):
    """单条 Provider 公共能力声明。"""

    model_config = ConfigDict(frozen=True)

    data_type: DataType
    markets: list[str]
    asset_types: list[AssetType]
    delivery: list[Delivery]
    intervals: list[IntervalEntry]
    adjustments: list[Adjust]
    fields: list[str]

    def supports(
        self,
        *,
        data_type: DataType,
        market: str,
        asset_type: AssetType,
        interval: Interval,
        adjust: Adjust,
    ) -> bool:
        """完整能力维度匹配：data_type × market × asset_type × interval × adjust。"""
        if data_type != self.data_type:
            return False
        if market not in self.markets:
            return False
        if asset_type not in self.asset_types:
            return False
        if not any(entry.code == interval for entry in self.intervals):
            return False
        return adjust in self.adjustments
