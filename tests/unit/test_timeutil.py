"""domain.timeutil 纪元转换测试：负时间戳与亚微秒截断。

背景：Windows 上 ``datetime.fromtimestamp`` 对 1970 年前的负时间戳抛
``OSError [Errno 22]``（CRT localtime 限制），探针实证上游确有更早历史
数据（TickFlow GE 1960 年代日线），各 Provider 映射统一走纪元算术。
"""

from datetime import datetime, timedelta, timezone

from tapehub.domain.models import CN_TIMEZONE
from tapehub.domain.timeutil import epoch_to_datetime


def _epoch_nanos(value: datetime) -> int:
    """aware datetime → 纪元纳秒（整数运算，不经 float 时间戳）。"""
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta // timedelta(microseconds=1) * 1_000


def test_pre_1970_negative_timestamp() -> None:
    nanos = _epoch_nanos(datetime(1960, 6, 15, tzinfo=timezone.utc))
    assert epoch_to_datetime(nanos, CN_TIMEZONE) == datetime(
        1960, 6, 15, 8, tzinfo=CN_TIMEZONE
    )


def test_epoch_boundary_and_sub_microsecond_truncation() -> None:
    assert epoch_to_datetime(0, CN_TIMEZONE) == datetime(
        1970, 1, 1, 8, tzinfo=CN_TIMEZONE
    )
    # 0.999999999s：纳秒截断到微秒 999999（ns // 1000），不足 1µs 丢弃
    assert epoch_to_datetime(999_999_999, timezone.utc) == datetime(
        1970, 1, 1, 0, 0, 0, 999_999, tzinfo=timezone.utc
    )
