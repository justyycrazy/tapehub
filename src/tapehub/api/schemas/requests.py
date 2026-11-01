"""历史 K 线查询的请求参数与校验。

校验职责：

- ``asset_type`` 必填，不允许默认推断；
- ``instrument`` 必须是标准 ``{MARKET}-{EXCHANGE}-{SYMBOL}`` 格式；
- ``interval`` 必须是合法 ISO 8601 Duration；
- 日期值（YYYY-MM-DD）按 UTC 0 点解释；带时间的值必须包含时区；
- ``start`` 必须早于 ``end``；时间范围为闭区间 [start, end]；
- ``limit`` 不超过服务配置上限；
- ``adjust`` 未传递时：个股（stock）默认 ``qfq``（前复权），其余默认 ``none``；
- ``variant`` 未传递时默认 ``eastmoney``（东方财富）。
"""

import re
from datetime import date, datetime, timezone

from pydantic import BaseModel, Field

from tapehub.domain.errors import InvalidRequestError
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, DataVariant, Instrument
from tapehub.settings import Settings

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_PAGE_LIMIT = 500


class BarsQueryParams(BaseModel):
    """``GET /v1/market/bars`` 查询参数。

    ``adjust`` 使用 None 作为「未传递」哨兵，由 ``to_bar_query`` 按
    资产类型填充声明默认值（个股 qfq，其余 none）。
    """

    provider: str = "auto"
    asset_type: AssetType
    instrument: str
    interval: str
    start: str
    end: str
    adjust: Adjust | None = None
    variant: DataVariant = DataVariant.EASTMONEY
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_PAGE_LIMIT, ge=1)


def effective_adjust(params: BarsQueryParams) -> Adjust:
    """未传递 adjust 时的声明默认值：个股前复权，其余不复权。"""
    if params.adjust is not None:
        return params.adjust
    if params.asset_type == AssetType.STOCK:
        return Adjust.QFQ
    return Adjust.NONE


def parse_boundary(value: str, name: str) -> datetime:
    """解析时间边界：仅日期按 UTC 0 点解释；带时间必须含时区。"""
    if _DATE_ONLY_RE.match(value):
        parsed_date = date.fromisoformat(value)
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidRequestError(
            "Invalid request parameters.",
            details={"param": name, "reason": f"{value!r} is not an ISO 8601 date or datetime"},
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise InvalidRequestError(
            "Invalid request parameters.",
            details={
                "param": name,
                "reason": (
                    "timezone is required for datetime values "
                    "(e.g. 2026-01-01T00:00:00+08:00)"
                ),
            },
        )
    return parsed.astimezone(timezone.utc)


def _invalid(param: str, reason: str) -> InvalidRequestError:
    return InvalidRequestError(
        "Invalid request parameters.",
        details={"param": param, "reason": reason},
    )


def to_bar_query(params: BarsQueryParams, settings: Settings) -> BarQuery:
    """把 API 查询参数转换为统一领域请求；失败时抛出统一参数错误。"""
    try:
        instrument = Instrument.parse(params.instrument)
    except ValueError as exc:
        raise _invalid("instrument", str(exc)) from exc

    try:
        interval = Interval.parse(params.interval)
    except ValueError as exc:
        raise _invalid("interval", str(exc)) from exc

    start = parse_boundary(params.start, "start")
    end = parse_boundary(params.end, "end")
    if start >= end:
        raise _invalid("start", "start must be earlier than end")

    if params.limit > settings.max_limit:
        raise _invalid("limit", f"limit must not exceed configured maximum {settings.max_limit}")

    return BarQuery(
        provider=params.provider,
        asset_type=params.asset_type,
        instrument=instrument,
        interval=interval,
        start=start,
        end=end,
        adjust=effective_adjust(params),
        variant=params.variant,
        offset=params.offset,
        limit=params.limit,
    )
