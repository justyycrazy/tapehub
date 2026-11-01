"""统一时间周期规范。

`interval` 直接使用 ISO 8601 Duration 作为周期代码：

- ``PT1M`` 表示 1 分钟，``P1M`` 表示 1 个月，两者是不同周期；
- 不把 ``P1M`` 换算为固定 30 天，也不把周期统一换算为秒数；
- 周期相等当且仅当各分量（年/月/周/日/时/分/秒）完全一致，
  因此 ``P1W`` 与 ``P7D``、``PT1H`` 与 ``PT60M`` 视为不同周期代码。
"""

import re
from dataclasses import dataclass

_DURATION_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)

_COMPONENTS = ("years", "months", "weeks", "days", "hours", "minutes", "seconds")


@dataclass(frozen=True, slots=True)
class Interval:
    """ISO 8601 Duration 周期，分量不跨单位换算。"""

    years: int = 0
    months: int = 0
    weeks: int = 0
    days: int = 0
    hours: int = 0
    minutes: int = 0
    seconds: int = 0

    def __post_init__(self) -> None:
        for name in _COMPONENTS:
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"interval component {name!r} must be non-negative")
        if not any(getattr(self, name) for name in _COMPONENTS):
            raise ValueError("interval must contain at least one positive component")

    @property
    def code(self) -> str:
        """规范化周期代码，例如 ``P1D``、``PT1M``、``P1Y2M``。"""
        date_part = ""
        if self.years:
            date_part += f"{self.years}Y"
        if self.months:
            date_part += f"{self.months}M"
        if self.weeks:
            date_part += f"{self.weeks}W"
        if self.days:
            date_part += f"{self.days}D"
        time_part = ""
        if self.hours:
            time_part += f"{self.hours}H"
        if self.minutes:
            time_part += f"{self.minutes}M"
        if self.seconds:
            time_part += f"{self.seconds}S"
        if not time_part:
            return f"P{date_part}"
        return f"P{date_part}T{time_part}"

    def __str__(self) -> str:
        return self.code

    @classmethod
    def parse(cls, value: str) -> "Interval":
        """解析 ISO 8601 Duration 字符串，非法输入抛出 ``ValueError``。"""
        if not isinstance(value, str):
            raise ValueError("interval must be a string")
        match = _DURATION_RE.match(value)
        if match is None:
            raise ValueError(f"invalid ISO 8601 duration: {value!r}")
        years, months, weeks, days, hours, minutes, seconds = (
            int(group) if group else 0 for group in match.groups()
        )
        if "T" in value and not any((hours, minutes, seconds)):
            raise ValueError(f"invalid ISO 8601 duration: {value!r} (empty time part)")
        return cls(
            years=years,
            months=months,
            weeks=weeks,
            days=days,
            hours=hours,
            minutes=minutes,
            seconds=seconds,
        )

    @classmethod
    def _coerce(cls, value: object) -> "Interval":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls.parse(value)
        raise ValueError("interval must be an ISO 8601 duration string or Interval instance")

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: object, handler: object) -> object:
        from pydantic_core import core_schema

        return core_schema.no_info_plain_validator_function(
            cls._coerce,
            serialization=core_schema.to_string_ser_schema(),
        )
