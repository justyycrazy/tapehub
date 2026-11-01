"""统一周期（ISO 8601 Duration）解析测试。"""

import pytest

from tapehub.domain.intervals import Interval


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("PT1S", "PT1S"),
        ("PT1M", "PT1M"),
        ("PT5M", "PT5M"),
        ("PT1H", "PT1H"),
        ("P1D", "P1D"),
        ("P1W", "P1W"),
        ("P1M", "P1M"),
        ("P1Y", "P1Y"),
        ("P1Y2M", "P1Y2M"),
        ("P1Y0M", "P1Y"),  # 零分量被规范化剔除
    ],
)
def test_parse_common_codes(raw: str, code: str) -> None:
    assert Interval.parse(raw).code == code


def test_minute_vs_month_distinct() -> None:
    minute = Interval.parse("PT1M")
    month = Interval.parse("P1M")
    assert minute != month
    assert minute.code != month.code
    # 时间分量与日期分量不换算：PT60M 与 PT1H 也视为不同周期代码
    assert Interval.parse("PT60M") != Interval.parse("PT1H")
    assert Interval.parse("P1W") != Interval.parse("P7D")


@pytest.mark.parametrize(
    "raw",
    [
        "1d",  # 非法自定义缩写
        "tick",  # tick 不是 ISO 8601 周期
        "",
        "P",
        "PT",
        "P1DT",
        "p1d",  # 小写
        "P-1D",
        "PT0.5S",  # 不支持小数
        "P0D",  # 全零
        "1D",
        "M1",
    ],
)
def test_reject_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        Interval.parse(raw)


def test_equality_by_components() -> None:
    assert Interval.parse("P1Y") == Interval.parse("P1Y")
    assert Interval.parse("P1M") == Interval(years=0, months=1)
    assert Interval.parse("PT1M30S") == Interval(minutes=1, seconds=30)
