"""call_in_thread 取消语义测试（M4，2026-11-13 审计修复）。

实测基准（anyio 4.15 / Python 3.12）：原始 ``asyncio`` 任务取消会穿透
shield 立即送达调用方——``abandon_on_cancel`` 的差异在于工作线程的归宿
（弃置继续跑 vs 线程池记账等待），而非调用方感知时机。因此本文件重点
验证服务实际依赖的两条语义：

- 可放弃：取消即时返回，线程后台跑完（结果丢弃）；
- 应用层 ``asyncio.wait_for`` 包裹（M2 超时路径）在线程长调用下按
  deadline 到点超时（504 即时语义），不被线程拖住。
"""

import asyncio
import threading
import time

import pytest

from tapehub.providers.base import call_in_thread


def test_abandon_on_cancel_returns_immediately() -> None:
    """可放弃：取消即时返回，线程后台跑完（结果丢弃）。"""
    release = threading.Event()

    def work() -> int:
        release.wait(5)  # 模拟长上游调用（baostock 无界 socket 形态）
        return 42

    async def scenario() -> None:
        task = asyncio.ensure_future(call_in_thread(work, abandon_on_cancel=True))
        await asyncio.sleep(0.05)  # worker 已进入 release.wait
        started = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.perf_counter() - started < 0.2  # 立即返回
        release.set()  # 释放工作线程（结果被丢弃）

    asyncio.run(scenario())
    release.set()  # 双保险：异常路径也不留挂死线程


def test_wait_for_over_call_in_thread_times_out_at_deadline() -> None:
    """M2 超时路径：wait_for 包裹线程长调用按 deadline 到点超时。

    线程内调用不被强杀（0.5s 自超时自然结束），但调用方在 0.05s 即收到
    ``TimeoutError``——应用层 504 语义不被慢线程拖住。
    """

    def work() -> int:
        done = threading.Event()
        done.wait(0.5)  # 自超时返回，主线程不干预
        return 42

    async def scenario() -> None:
        started = time.perf_counter()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(call_in_thread(work), 0.05)
        assert time.perf_counter() - started < 0.2  # 到点超时而非等线程

    asyncio.run(scenario())
