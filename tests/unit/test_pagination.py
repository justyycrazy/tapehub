"""共享分页辅助组件测试。"""

from datetime import datetime, timezone

from tapehub.domain.models import Bar
from tapehub.providers.pagination import paginate_bars, sort_bars

UTC = timezone.utc


def _bar(day: int) -> Bar:
    return Bar(timestamp=datetime(2026, 1, day, tzinfo=UTC), close=float(day))


def test_sort_ascending_does_not_mutate_input() -> None:
    bars = [_bar(3), _bar(1), _bar(2)]
    ordered = sort_bars(bars)
    assert [b.timestamp.day for b in ordered] == [1, 2, 3]
    assert [b.timestamp.day for b in bars] == [3, 1, 2]


def test_paginate_window() -> None:
    bars = [_bar(day) for day in range(1, 11)]
    page = paginate_bars(bars, offset=2, limit=3)
    assert [b.timestamp.day for b in page] == [3, 4, 5]


def test_paginate_offset_beyond_end() -> None:
    bars = [_bar(day) for day in range(1, 4)]
    assert paginate_bars(bars, offset=10, limit=5) == []


def test_paginate_partial_tail() -> None:
    bars = [_bar(day) for day in range(1, 6)]
    page = paginate_bars(bars, offset=3, limit=10)
    assert [b.timestamp.day for b in page] == [4, 5]
