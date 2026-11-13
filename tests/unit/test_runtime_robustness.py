"""运行时健壮性补强测试（批次 3，2026-11-13 审计低危项）。

- tqsdk ``_fetch_coro``：已取消请求早退（不消耗 SDK 协程）；
- ``_StreamIterator``：在飞 ``__anext__`` 下 aclose 不抛 already-executing、
  未消费即被丢弃时 GC 兜底释放配额；
- twelvedata Runtime 内部任务（发送）持有引用并在完成后摘除。
"""

import asyncio
import concurrent.futures
from typing import Any

import pytest

from tapehub.application.quote_stream_service import _StreamIterator
from tapehub.providers.tqsdk.runtime import TqSdkRuntime
from tapehub.providers.twelvedata.runtime import TwelvedataWsRuntime

# ---------------------------------------------------------------------------
# tqsdk _fetch_coro cancelled 早退
# ---------------------------------------------------------------------------


class _RecordingApi:
    """最小 TqApi 桩：记录 get_kline_serial 调用并返回可转 dict 的行。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_kline_serial(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Frame:
            def to_dict(self, orient: str) -> list[dict[str, Any]]:
                assert orient == "records"
                return [{"close": 1.0}]

        return _Frame()


def test_fetch_coro_skips_cancelled_request() -> None:
    """已取消的请求（wrap_future 反向传播）早退：不调用 SDK、不回填。"""
    runtime = TqSdkRuntime(auth_user="u", auth_password="p")
    api = _RecordingApi()

    # 正常请求：调用 SDK 并回填结果
    cf = concurrent.futures.Future()
    asyncio.run(runtime._fetch_coro(api, {"symbol": "SHFE.cu"}, cf))
    assert len(api.calls) == 1
    assert cf.result() == [{"close": 1.0}]

    # 已取消请求：早退——不再调用 SDK
    cancelled = concurrent.futures.Future()
    assert cancelled.cancel()
    asyncio.run(runtime._fetch_coro(api, {"symbol": "SHFE.cu"}, cancelled))
    assert len(api.calls) == 1  # 未新增
    assert cancelled.cancelled()


# ---------------------------------------------------------------------------
# _StreamIterator：在飞 aclose 与 GC 兜底
# ---------------------------------------------------------------------------


async def _scenario_inflight_aclose() -> list[int]:
    released: list[int] = []

    async def gen() -> Any:
        yield 1
        await asyncio.sleep(10)  # 第二读挂在此处（模拟慢上游/慢客户端）
        yield 2

    def release() -> None:
        # 模拟真实 _release(slot) 的幂等语义（released 标志）：
        # aclose finally 与 __del__ 兜底可能都触发，但配额只释放一次
        if not released:
            released.append(1)

    it = _StreamIterator(gen(), release)
    first = await it.__anext__()  # 消费首个事件（=1），生成器停在 yield 1 后
    assert first == 1
    task = asyncio.ensure_future(it.__anext__())  # 挂在 sleep(10)
    await asyncio.sleep(0.02)
    await it.aclose()  # 不应抛「generator is already executing」
    with pytest.raises(asyncio.CancelledError):
        await task  # 在飞读已被取消
    return released


def test_stream_iterator_aclose_with_inflight_next() -> None:
    """另一任务挂在 __anext__ 上时 aclose：先取消等传播完成，再释放。"""
    released = asyncio.run(_scenario_inflight_aclose())
    assert released == [1]  # finally/GC 兜底幂等释放


def test_stream_iterator_gc_release_without_consumption() -> None:
    """受理后从未被消费即被丢弃：__del__ 兜底释放配额（幂等）。"""
    released: list[int] = []

    async def gen() -> Any:
        yield 1  # pragma: no cover - 永不被消费

    it = _StreamIterator(gen(), lambda: released.append(1))
    del it  # 触发引用计数回收 → __del__ → release
    assert released == [1]


# ---------------------------------------------------------------------------
# twelvedata 内部任务持有引用
# ---------------------------------------------------------------------------


def test_twelvedata_send_task_tracked_and_reaped() -> None:
    """_send 的裸任务入集合持有引用，完成后自动摘除（防 GC 概率销毁）。"""
    runtime = TwelvedataWsRuntime(api_key="k", subscription_limit=8)
    sent: list[dict[str, Any]] = []

    class _FakeWs:
        async def send(self, text: str) -> None:
            import json

            sent.append(json.loads(text))

    async def scenario() -> None:
        runtime._ws = _FakeWs()
        runtime._send({"action": "subscribe", "params": {"symbol": "EUR/USD"}})
        assert len(runtime._tasks) == 1  # 任务挂起期间被持有
        await asyncio.sleep(0.05)
        assert sent == [{"action": "subscribe", "params": {"symbol": "EUR/USD"}}]
        assert runtime._tasks == set()  # 完成后自动摘除

    asyncio.run(scenario())
