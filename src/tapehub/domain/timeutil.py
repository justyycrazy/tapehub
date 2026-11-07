"""纪元时间戳 → aware datetime 的共享转换。

各 Provider 响应映射统一走此处的纪元算术，不用 ``datetime.fromtimestamp``：
后者在 Windows 上对 1970 年前的负时间戳抛 ``OSError [Errno 22]``（CRT
localtime 限制），而上游确有更早的历史数据（TickFlow 探针实证 GE 1960 年代
日线，负毫秒正常出现）。纪元算术平台无关，正负时间戳行为一致。
"""

from datetime import datetime, timedelta, timezone, tzinfo

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


def epoch_to_datetime(epoch_nanos: int, tz: tzinfo) -> datetime:
    """纪元纳秒时间戳（可为负）→ 目标时区 aware datetime。

    亚微秒部分截断（与各 Provider 原实现的微秒精度一致）。
    """
    seconds, remainder_ns = divmod(int(epoch_nanos), 1_000_000_000)
    utc = _EPOCH_UTC + timedelta(seconds=seconds, microseconds=remainder_ns // 1000)
    return utc.astimezone(tz)
