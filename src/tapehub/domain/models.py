"""统一领域模型。

Domain 不依赖 FastAPI、PyYAML 或任何 Provider SDK；
时间字段一律要求带时区，对外统一转换为 UTC。
"""

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from tapehub.domain.intervals import Interval

UTC = timezone.utc

# 标准标的代码：{market}-{exchange}-{symbol}，例如 CN-SSE-600000。
# market/exchange 必须大写；symbol 大小写遵从交易所/Provider 约定
#（如 TqSdk 期货品种代码为小写 cu），允许大小写字母与 . _ -。
_INSTRUMENT_RE = re.compile(
    r"^([A-Z][A-Z0-9]{0,7})-([A-Z][A-Z0-9]{0,15})-([A-Za-z0-9][A-Za-z0-9._-]{0,31})$"
)


class AssetType(StrEnum):
    """资产类型。请求必须显式传递，不做默认推断。

    分类体系（category → asset_type）：

    - securities 证券：stock（个股）、index（指数）、etf
    - futures 期货：contract（单一合约）、continuous（主力连续）、weighted-index（加权）
    - options 期权：option
    - forex 外汇：forex
    - crypto 加密货币：crypto
    """

    ETF = "etf"
    INDEX = "index"
    STOCK = "stock"
    CONTRACT = "contract"
    CONTINUOUS = "continuous"
    WEIGHTED_INDEX = "weighted-index"
    OPTION = "option"
    FOREX = "forex"
    CRYPTO = "crypto"


class DataVariant(StrEnum):
    """数据源变体：同一 Provider 下同一资产类型的多个上游接口。

    取值是中立的站点名而不是上游函数名；具体映射由各 Provider 的
    manifest operations 完成。默认使用东方财富（eastmoney）。
    """

    EASTMONEY = "eastmoney"
    SINA = "sina"
    TENCENT = "tencent"


DEFAULT_VARIANT = DataVariant.EASTMONEY


class Adjust(StrEnum):
    """复权方式。"""

    NONE = "none"
    QFQ = "qfq"
    HFQ = "hfq"


class Delivery(StrEnum):
    """数据交付方式。"""

    REST = "rest"


class DataType(StrEnum):
    """数据类型。第一版仅历史 K 线。"""

    BARS = "bars"


def ensure_utc(value: datetime, field: str) -> datetime:
    """要求时区感知并统一转换为 UTC。"""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


class Instrument(BaseModel):
    """标准标的：{market}-{exchange}-{symbol}。"""

    model_config = ConfigDict(frozen=True)

    market: str
    exchange: str
    symbol: str

    @model_validator(mode="before")
    @classmethod
    def _parse_code(cls, value: object) -> object:
        # 允许字段直接接收标准代码字符串，如 instrument="CN-SSE-600000"
        if isinstance(value, str):
            match = _INSTRUMENT_RE.match(value)
            if match is None:
                raise ValueError(
                    f"invalid instrument {value!r}: expected '{{MARKET}}-{{EXCHANGE}}-{{SYMBOL}}' "
                    "(e.g. CN-SSE-600000, uppercase)"
                )
            market, exchange, symbol = match.groups()
            return {"market": market, "exchange": exchange, "symbol": symbol}
        return value

    @property
    def code(self) -> str:
        return f"{self.market}-{self.exchange}-{self.symbol}"

    def __str__(self) -> str:
        return self.code

    @classmethod
    def parse(cls, value: str) -> "Instrument":
        """解析标准标的代码，非法输入抛出 ``ValueError``。"""
        if not isinstance(value, str):
            raise ValueError("instrument must be a string")
        match = _INSTRUMENT_RE.match(value)
        if match is None:
            raise ValueError(
                f"invalid instrument {value!r}: expected '{{MARKET}}-{{EXCHANGE}}-{{SYMBOL}}' "
                "(e.g. CN-SSE-600000, uppercase)"
            )
        market, exchange, symbol = match.groups()
        return cls(market=market, exchange=exchange, symbol=symbol)


class Bar(BaseModel):
    """单根标准化 K 线。缺失数值使用 null，不得用 0 冒充。"""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    amount: float | None = None
    # 受控的 Provider 特有字段：适配器必须显式白名单后放入，不得无约束混入。
    extensions: dict[str, Any] | None = None

    @field_validator("timestamp")
    @classmethod
    def _timestamp_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value, "timestamp")


class BarQuery(BaseModel):
    """统一历史 K 线查询。时间范围采用闭区间 [start, end]。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = "auto"
    asset_type: AssetType
    instrument: Instrument
    interval: Interval
    start: datetime
    end: datetime
    adjust: Adjust = Adjust.NONE
    # 数据源变体，默认东方财富；Provider 专用映射在适配器内完成
    variant: DataVariant = DEFAULT_VARIANT
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=500, ge=1)

    @field_validator("start", "end")
    @classmethod
    def _bound_utc(cls, value: datetime, info: object) -> datetime:
        name = getattr(info, "field_name", "boundary")
        return ensure_utc(value, name)

    @model_validator(mode="after")
    def _start_before_end(self) -> "BarQuery":
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        return self


class BarBatch(BaseModel):
    """标准化 K 线批次。

    `offset`/`limit` 描述统一接口最终结果的分页语义：
    适配器返回前必须完成排序与切片，Application 层不得再次切片。
    """

    model_config = ConfigDict(extra="forbid")

    instrument: Instrument
    interval: Interval
    bars: list[Bar]
    # 实际执行的数据源，不能继续返回 "auto"。
    provider: str
    source_time: datetime | None = None
    received_at: datetime
    partial: bool = False
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)

    @field_validator("received_at")
    @classmethod
    def _received_utc(cls, value: datetime) -> datetime:
        return ensure_utc(value, "received_at")

    @field_validator("source_time")
    @classmethod
    def _source_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value, "source_time")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def count(self) -> int:
        """本次实际返回的 K 线数量。"""
        return len(self.bars)
