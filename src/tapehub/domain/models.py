"""统一领域模型。

Domain 不依赖 FastAPI、PyYAML 或任何 Provider SDK；
时间字段一律要求带时区偏移，输出按标的所属市场的规范时区：
国内（CN）行情统一北京时间（+08:00），其余市场统一 UTC。
"""

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from tapehub.domain.intervals import Interval

UTC = timezone.utc
# 国内行情数据的规范时区：中国无夏令时，Asia/Shanghai 恒为 +08:00
CN_TIMEZONE = ZoneInfo("Asia/Shanghai")


def market_zone(market: str) -> ZoneInfo:
    """市场的规范时区：国内（CN）→ 北京时间，其余市场 → UTC。"""
    return CN_TIMEZONE if market == "CN" else UTC


def ensure_aware(value: datetime, field: str) -> datetime:
    """要求时区感知（带时区偏移），原样保留偏移不做换算。"""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def normalize_market_time(value: datetime, market: str, field: str) -> datetime:
    """要求时区感知并归一化到该市场的规范时区（CN → 北京时间，其余 → UTC）。"""
    return ensure_aware(value, field).astimezone(market_zone(market))

# 标准标的代码：{market}-{exchange}-{symbol}，例如 CN-SSE-600000。
# market/exchange 必须大写；symbol 大小写与字符集遵从交易所/Provider 约定：
# 允许大小写字母、数字与 . _ - /（斜杠用于 Twelve Data 的货币对，
# 如 GLOBAL-OTC-EUR/USD）。
_INSTRUMENT_RE = re.compile(
    r"^([A-Z][A-Z0-9]{0,7})-([A-Z][A-Z0-9]{0,15})-([A-Za-z0-9/][A-Za-z0-9/._-]{0,31})$"
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

    取值是中立的数据源站点名而不是上游函数名；具体映射由各 Provider 的
    manifest operations 完成。默认使用东方财富（eastmoney）。
    """

    EASTMONEY = "eastmoney"
    SINA = "sina"
    TENCENT = "tencent"
    # TqSdk（快期 Shinny Tech 官方 SDK 包名，背后为天勤 TianQin 行情平台）
    TQSDK = "tqsdk"
    # 以下为独立数据服务体系，数据源即上游服务自身，与东方财富无关
    TUSHARE = "tushare"
    ALPHAFEED = "alphafeed"
    TICKFLOW = "tickflow"
    TWELVEDATA = "twelvedata"


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
    """单根标准化 K 线。缺失数值使用 null，不得用 0 冒充。

    ``timestamp`` 只要求时区感知（偏移原样保留）；市场规范时区的归一化
    统一由所属 ``BarBatch`` 完成（Bar 自身不携带市场上下文）。
    """

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
    def _timestamp_aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "timestamp")


class BarQuery(BaseModel):
    """统一历史 K 线查询。时间范围采用闭区间 [start, end]。

    输入边界只要求时区感知（偏移任意，CN 标的推荐 +08:00）；
    构造完成后统一归一化到标的市场的规范时区（CN → 北京时间）。
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = "auto"
    asset_type: AssetType
    instrument: Instrument
    interval: Interval
    start: datetime
    end: datetime
    adjust: Adjust = Adjust.NONE
    # 数据源变体；未传递（None）表示不表达偏好，由所选 Provider 能力的
    # default_variant 具体化（见 application 路由），适配器收到的一律是具体值
    variant: DataVariant | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=500, ge=1)

    @field_validator("start", "end")
    @classmethod
    def _bound_aware(cls, value: datetime, info: object) -> datetime:
        name = getattr(info, "field_name", "boundary")
        return ensure_aware(value, name)

    @model_validator(mode="after")
    def _start_before_end(self) -> "BarQuery":
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        return self

    @model_validator(mode="after")
    def _bounds_market_zone(self) -> "BarQuery":
        zone = market_zone(self.instrument.market)
        self.start = self.start.astimezone(zone)
        self.end = self.end.astimezone(zone)
        return self


class BarBatch(BaseModel):
    """标准化 K 线批次。

    `offset`/`limit` 描述统一接口最终结果的分页语义：
    适配器返回前必须完成排序与切片，Application 层不得再次切片。

    构造完成后，批次内全部时间字段（bar timestamp、source_time、received_at）
    统一归一化到标的市场的规范时区（CN → 北京时间，其余 → UTC）。
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
    def _received_aware(cls, value: datetime) -> datetime:
        return ensure_aware(value, "received_at")

    @field_validator("source_time")
    @classmethod
    def _source_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ensure_aware(value, "source_time")

    @model_validator(mode="after")
    def _market_zone(self) -> "BarBatch":
        zone = market_zone(self.instrument.market)
        self.received_at = self.received_at.astimezone(zone)
        if self.source_time is not None:
            self.source_time = self.source_time.astimezone(zone)
        for bar in self.bars:
            bar.timestamp = bar.timestamp.astimezone(zone)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def count(self) -> int:
        """本次实际返回的 K 线数量。"""
        return len(self.bars)
