"""Provider 能力模型。

能力目录是路由与能力查询接口共用的唯一事实来源，
避免“能力接口显示支持，但实际路由不支持”的不一致。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, DataType, DataVariant, Delivery, ensure_aware


class IntervalHistory(BaseModel):
    """已知的历史数据覆盖范围；无法确定时保持 null，不得编造。

    ``start`` 为固定起点（适合静态覆盖）；``retention_days`` 为滚动保留天数
    （覆盖「now − N 天 → now」，边界随时间前移，适合分钟线等滚动保留场景）。
    两者互斥，可同时为 null（覆盖未知）。

    边界只要求时区感知；到市场规范时区的归一化由 manifest 加载层
    （知晓所属市场）完成，直接构造 Capability 的调用方需自行保证。
    """

    model_config = ConfigDict(frozen=True)

    start: datetime | None = None
    end: datetime | None = None
    retention_days: int | None = Field(default=None, ge=1)

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_aware(value, "history boundary")

    @model_validator(mode="after")
    def _start_and_retention_mutually_exclusive(self) -> "IntervalHistory":
        if self.start is not None and self.retention_days is not None:
            raise ValueError("history start and retention_days are mutually exclusive")
        return self


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
    variants: list[DataVariant]
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
        variant: DataVariant | None,
    ) -> bool:
        """完整能力维度匹配：data_type × market × asset_type × interval × adjust × variant。

        ``variant=None`` 表示请求未表达变体偏好，该维度不参与匹配；
        具体化（默认变体解析）由 ProviderRecord 完成，本方法不做默认值推断。
        """
        if data_type != self.data_type:
            return False
        if market not in self.markets:
            return False
        if asset_type not in self.asset_types:
            return False
        if variant is not None and variant not in self.variants:
            return False
        if not any(entry.code == interval for entry in self.intervals):
            return False
        return adjust in self.adjustments
