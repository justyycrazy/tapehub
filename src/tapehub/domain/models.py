"""统一领域模型。

Domain 不依赖 FastAPI、PyYAML 或任何 Provider SDK；
时间字段一律要求带时区偏移，全市场统一归一化为北京时间（+08:00）
（2026-11-04 数据探针确认上游口径后统一，见 README「时间边界与时区」）。
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from tapehub.domain.intervals import Interval

# 项目规范时区：中国无夏令时，Asia/Shanghai 恒为 +08:00；全市场统一使用
CN_TIMEZONE = ZoneInfo("Asia/Shanghai")


def ensure_aware(value: datetime, field: str) -> datetime:
    """要求时区感知（带时区偏移），原样保留偏移不做换算。"""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field} must include a timezone")
    return value

# 标准标的代码：{market}-{exchange}-{symbol}，规范形式全大写（CN-SSE-600000）。
# 输入大小写不敏感：解析时三段统一大写归一，Instrument 字段恒为规范大写；
# 期货品种等需要交易所原生大小写的上游（如 tqsdk 的 SHFE.cu2607）由各
# Provider 适配器自行还原。字符集允许字母、数字与 . _ - /（斜杠用于
# Twelve Data 的货币对，如 GLOBAL-OTC-EUR/USD）。
_INSTRUMENT_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9]{0,7})-([A-Za-z][A-Za-z0-9]{0,15})-([A-Za-z0-9/][A-Za-z0-9/._-]{0,31})$"
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
    TIINGO = "tiingo"
    TWELVEDATA = "twelvedata"
    BAOSTOCK = "baostock"
    # 同花顺金融数据 API（fuyao.aicubes.cn）：数据源即上游服务自身
    THS = "ths"
    # 富途 OpenAPI HTTP 直连（webapi.futunn.com）：数据源即上游服务自身
    FUTUNN = "futunn"


class Adjust(StrEnum):
    """复权方式。"""

    NONE = "none"
    QFQ = "qfq"
    HFQ = "hfq"


class Delivery(StrEnum):
    """数据交付方式。"""

    REST = "rest"


class DataType(StrEnum):
    """数据类型：历史 K 线与实时快照（quote 无周期/复权维度）。"""

    BARS = "bars"
    QUOTE = "quotes"


def _code_to_fields(value: str) -> dict[str, str]:
    """标准代码字符串 → 三段字段（统一大写归一），非法输入抛 ``ValueError``。"""
    match = _INSTRUMENT_RE.match(value)
    if match is None:
        raise ValueError(
            f"invalid instrument {value!r}: expected '{{MARKET}}-{{EXCHANGE}}-{{SYMBOL}}' "
            "(e.g. CN-SSE-600000; case-insensitive, normalized to uppercase)"
        )
    market, exchange, symbol = match.groups()
    return {
        "market": market.upper(),
        "exchange": exchange.upper(),
        "symbol": symbol.upper(),
    }


class Instrument(BaseModel):
    """标准标的：{market}-{exchange}-{symbol}，三段统一大写（大小写不敏感输入）。"""

    model_config = ConfigDict(frozen=True)

    market: str
    exchange: str
    symbol: str

    @model_validator(mode="before")
    @classmethod
    def _parse_code(cls, value: object) -> object:
        # 允许字段直接接收标准代码字符串，如 instrument="CN-SSE-600000"
        # 或任意大小写变体（cn-sse-cu2607）；字符串与字段直构两条路径都
        # 大写归一，保证不变式：Instrument 三段恒为规范大写
        if isinstance(value, str):
            return _code_to_fields(value)
        if isinstance(value, dict):
            normalized = dict(value)
            for field in ("market", "exchange", "symbol"):
                item = normalized.get(field)
                if isinstance(item, str):
                    normalized[field] = item.upper()
            return normalized
        return value

    @property
    def code(self) -> str:
        return f"{self.market}-{self.exchange}-{self.symbol}"

    def __str__(self) -> str:
        return self.code

    @classmethod
    def parse(cls, value: str) -> "Instrument":
        """解析标准标的代码（大小写不敏感，归一为全大写），非法输入抛 ``ValueError``。"""
        if not isinstance(value, str):
            raise ValueError("instrument must be a string")
        return cls(**_code_to_fields(value))


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

    输入边界只要求时区感知（偏移任意，推荐 +08:00）；
    构造完成后全市场统一归一化为北京时间（+08:00）。
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
        # 全市场统一北京时间（含 GLOBAL：twelvedata 外汇/加密货币等）
        self.start = self.start.astimezone(CN_TIMEZONE)
        self.end = self.end.astimezone(CN_TIMEZONE)
        return self


class BarBatch(BaseModel):
    """标准化 K 线批次。

    `offset`/`limit` 描述统一接口最终结果的分页语义：
    适配器返回前必须完成排序与切片，Application 层不得再次切片。

    构造完成后，批次内全部时间字段（bar timestamp、source_time、received_at）
    全市场统一归一化为北京时间（+08:00）。
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
        # 全市场统一北京时间（含 GLOBAL：twelvedata 外汇/加密货币等）
        self.received_at = self.received_at.astimezone(CN_TIMEZONE)
        if self.source_time is not None:
            self.source_time = self.source_time.astimezone(CN_TIMEZONE)
        for bar in self.bars:
            bar.timestamp = bar.timestamp.astimezone(CN_TIMEZONE)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def count(self) -> int:
        """本次实际返回的 K 线数量。"""
        return len(self.bars)


class Quote(BaseModel):
    """单标的实时快照。缺失数值使用 null，不得用 0 冒充。

    五档买卖盘各 5 档（price/volume 成对）；指数类标的无盘口时
    价格为 null、量为 0（如实透出，不编造）。``timestamp`` 为行情时间，
    构造时要求时区感知并统一归一化为北京时间（+08:00）——快照无批次
    包装，模型自身完成归一化。Provider 特有字段只经 ``extensions``
    白名单透出（tqsdk 白名单以 P5 探针实证为准，见 README 映射表）。
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    last_price: float | None = None
    bid_price1: float | None = None
    bid_volume1: float | None = None
    bid_price2: float | None = None
    bid_volume2: float | None = None
    bid_price3: float | None = None
    bid_volume3: float | None = None
    bid_price4: float | None = None
    bid_volume4: float | None = None
    bid_price5: float | None = None
    bid_volume5: float | None = None
    ask_price1: float | None = None
    ask_volume1: float | None = None
    ask_price2: float | None = None
    ask_volume2: float | None = None
    ask_price3: float | None = None
    ask_volume3: float | None = None
    ask_price4: float | None = None
    ask_volume4: float | None = None
    ask_price5: float | None = None
    ask_volume5: float | None = None
    volume: float | None = None
    amount: float | None = None
    # 实际执行的数据源（同 BarBatch.provider 语义，不能是 "auto"）
    provider: str
    # 快照完成标准化（或从缓存读出）的网关时刻
    received_at: datetime
    # 受控的 Provider 特有字段：适配器必须显式白名单后放入，不得无约束混入。
    extensions: dict[str, Any] | None = None

    @field_validator("timestamp", "received_at")
    @classmethod
    def _aware(cls, value: datetime, info: object) -> datetime:
        name = getattr(info, "field_name", "timestamp")
        return ensure_aware(value, name)

    @model_validator(mode="after")
    def _market_zone(self) -> "Quote":
        # 全市场统一北京时间（快照时间戳为行情时间）
        self.timestamp = self.timestamp.astimezone(CN_TIMEZONE)
        self.received_at = self.received_at.astimezone(CN_TIMEZONE)
        return self


class QuoteQuery(BaseModel):
    """统一实时快照查询。快照是当前时点的订阅数据：无周期、无时间范围、
    无分页；``asset_type`` 同样必填不推断。"""

    model_config = ConfigDict(extra="forbid")

    provider: str = "auto"
    asset_type: AssetType
    instrument: Instrument
    # 数据源变体；未传递（None）表示不表达偏好，由所选 Provider 能力的
    # default_variant 具体化（见 application 路由），适配器收到的一律是具体值
    variant: DataVariant | None = None


class StreamStatusKind(StrEnum):
    """行情流状态事件（SSE ``event: status`` 的取值）。

    断线与代际回收必须有明确事件，绝不静默断流：
    """

    # 订阅建立、上游首包到达（含代际回收后自动重订完成）
    SUBSCRIBED = "subscribed"
    # 上游连接异常（wait_update 失败；服务停机时同以此事件终结流）
    UPSTREAM_DISCONNECTED = "upstream_disconnected"
    # Runtime 新一代重建中（北京时间 04:00 跨交易日回收 / watchdog / 休眠
    # 订阅回收阈值），活跃订阅将在新一代自动重订
    RECYCLING = "recycling"
    # 流空闲超时，服务端本地退订并结束流（上游订阅保留至回收，见 README）
    IDLE_UNSUBSCRIBED = "idle_unsubscribed"


class QuoteStreamEvent(BaseModel):
    """行情流事件：``quote``（标准快照）、``status``（流状态）与 ``heartbeat``
    （空闲保活帧）三选一。"""

    model_config = ConfigDict(extra="forbid")

    quote: Quote | None = None
    status: StreamStatusKind | None = None
    heartbeat: bool = False

    @model_validator(mode="after")
    def _exactly_one(self) -> "QuoteStreamEvent":
        filled = sum(1 for item in (self.quote, self.status, self.heartbeat) if item)
        if filled != 1:
            raise ValueError("exactly one of quote / status / heartbeat must be set")
        return self
