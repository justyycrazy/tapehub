"""共享分页辅助组件。

统一分页语义：先按 ``timestamp`` 升序排序，再应用 ``offset`` 与 ``limit``。
Provider 原生不支持分页时，适配器必须使用本组件在内存中切片，
不得落盘，也不得把 Provider 自有分页语义泄露到统一接口。
"""

from tapehub.domain.models import Bar


def sort_bars(bars: list[Bar]) -> list[Bar]:
    """按 timestamp 升序返回新列表，不修改入参。"""
    return sorted(bars, key=lambda bar: bar.timestamp)


def paginate_bars(bars: list[Bar], offset: int, limit: int) -> list[Bar]:
    """排序后切片，返回 [offset, offset + limit) 窗口。"""
    ordered = sort_bars(bars)
    return ordered[offset : offset + limit]
