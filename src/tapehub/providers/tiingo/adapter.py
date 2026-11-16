"""Tiingo 适配器（已实现：历史 K 线 REST + 实时快照/快照流 WS）。

映射规则与上游行为定案见 docs/providers/tiingo.md（唯一出处，本文不复述）。
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    QuoteNotReadyError,
    UpstreamError,
    UpstreamRateLimitedError,
    UpstreamSubscriptionLimitError,
)
from tapehub.domain.intervals import Interval
from tapehub.domain.models import (
    CN_TIMEZONE,
    AssetType,
    BarBatch,
    BarQuery,
    Quote,
    QuoteQuery,
    QuoteStreamEvent,
    StreamStatusKind,
)
from tapehub.providers.base import BaseProviderAdapter, call_in_thread
from tapehub.providers.pagination import paginate_bars
from tapehub.providers.stream_buffer import StreamBuffer
from tapehub.providers.tiingo.request_mapper import (
    FX_ARCHIVE_FLOOR,
    UPSTREAM_NO_DATA_FINGERPRINT,
    build_request,
    tiingo_symbol,
)
from tapehub.providers.tiingo.response_mapper import to_standard_bars, ws_price_to_quote
from tapehub.providers.tiingo.runtime import TiingoWsRuntime

if TYPE_CHECKING:
    from tapehub.settings import Settings

# httpx2 客户端超时（秒）：应用层 upstream_timeout_seconds（默认 30s）为
# 整体兜底，客户端自身超时取更小的保守值，避免长挂线程占用 worker
_HTTP_TIMEOUT_SECONDS = 20.0

# fx PT1M 分段判据常量（见 _fetch_sync 注释）：仅 fx 一分钟周期适用
_FX_ONE_MIN = Interval.parse("PT1M")
# 尾部缺失容差：段长 ≈6 自然日 + 周末/假日 1 天余量（窗口 ≤7 天不误报）
_FX_SEGMENT_TOLERANCE = timedelta(days=7)
# crypto 端点段上限（一周/一月/段后窗均恰 5001 行，alphafeed 式
# count>=MAX 判定）——PT1M ≈3.5 天/段、PT1H ≈208 天、PT2H ≈416 天、
# PT4H/P1D 远超实用窗长
_CRYPTO_MAX_ROWS = 5001

# 声明 quotes 能力的资产类型（WS 端点分流；与 manifest 声明一致）
_QUOTE_ASSET_TYPES = ("forex", "crypto")


class ProviderAdapter(BaseProviderAdapter):
    """Tiingo 适配器。"""

    implemented = True

    def __init__(self, name: str, manifest: Any, settings: "Settings | None" = None) -> None:
        super().__init__(name, manifest, settings)
        # crypto / forex 各一个懒启动 WS Runtime（端点与 thresholdLevel 分流）
        self._runtimes: dict[str, TiingoWsRuntime] = {}
        self._runtime_lock = asyncio.Lock()

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        api_key = self._ensure_ready()
        return await call_in_thread(
            self._fetch_sync, api_key, operation, query, abandon_on_cancel=True
        )

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        """实时快照：WS 订阅缓存（twelvedata 同构）——活跃订阅读缓存，
        冷订阅等首包、超时抛 ``QUOTE_NOT_READY``，取数后引用归零。"""
        self.resolve_quote_operation(query)
        api_key = self._ensure_ready()
        symbol = tiingo_symbol(query)
        runtime = await self._ensure_runtime(api_key, query.asset_type.value)
        self._ensure_capacity(runtime, symbol)
        runtime.subscribe(symbol)
        try:
            snapshot = await self._await_snapshot(runtime, symbol)
        finally:
            runtime.unsubscribe(symbol)
        return ws_price_to_quote(
            snapshot, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def stream_quote(self, query: QuoteQuery) -> AsyncIterator[QuoteStreamEvent]:
        """实时快照流（M3）：WS 订阅 + 监听器 → 有界队列 → 事件流。

        受理校验急切执行（错误在响应头发出前统一抛出）；Runtime 启动与
        订阅在首次迭代时进行，断线/重订恢复由 Runtime 以显式 status 事件
        通知。
        """
        self.resolve_quote_operation(query)
        api_key = self._ensure_ready()
        symbol = tiingo_symbol(query)
        asset_type = query.asset_type.value
        runtime = self._runtimes.get(asset_type)
        self._ensure_capacity(runtime, symbol)
        return self._stream_events(api_key, asset_type, symbol)

    async def _stream_events(
        self, api_key: str, asset_type: str, symbol: str
    ) -> AsyncIterator[QuoteStreamEvent]:
        runtime = await self._ensure_runtime(api_key, asset_type)
        runtime.subscribe(symbol)
        buffer = StreamBuffer(self._stream_queue_size())

        def _publish(kind: str, _symbol: str, snapshot: dict[str, Any] | None) -> None:
            """Runtime 回调（同事件循环）：直接投递进本流的有界缓冲。"""
            buffer.put(kind, snapshot)

        remove_listener = runtime.add_stream_listener(symbol, _publish)
        try:
            while True:
                kind, snapshot = await buffer.get()
                if kind == "quote":
                    if snapshot is not None:
                        yield QuoteStreamEvent(quote=self._quote_event(snapshot))
                else:
                    yield QuoteStreamEvent(status=StreamStatusKind(kind))
                    if kind == "subscribed" and snapshot is not None:
                        # 订阅确认先于首帧快照（首事件 status，随后 quote）
                        yield QuoteStreamEvent(quote=self._quote_event(snapshot))
        finally:
            remove_listener()
            runtime.unsubscribe(symbol)

    def _quote_event(self, snapshot: dict[str, Any]) -> Quote:
        """流帧快照标准化（与 M2 快照同 mapper、同北京时间口径）。"""
        return ws_price_to_quote(
            snapshot, provider=self.name, received_at=datetime.now(CN_TIMEZONE)
        )

    def shutdown_runtime(self) -> object | None:
        """停机钩子（由应用 lifespan 调用）：关闭已启动的 WS Runtime。

        有运行循环时返回异步停机任务由 lifespan await 到完成；无运行循环
        的兜底路径同步执行并返回 None。
        """
        runtimes = list(self._runtimes.values())
        if not runtimes:
            return None
        self._runtimes.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            return loop.create_task(
                self._shutdown_all(runtimes), name="tiingo-ws-shutdown"
            )
        for runtime in runtimes:  # 无运行循环的兜底（正常不触发）
            asyncio.run(runtime.shutdown())
        return None

    @staticmethod
    async def _shutdown_all(runtimes: list[TiingoWsRuntime]) -> None:
        for runtime in runtimes:
            await runtime.shutdown()

    def _ensure_ready(self) -> str:
        """校验运行前提：API Key 配置。"""
        value = getattr(self.settings, "tiingo_api_key", "") if self.settings else ""
        if not value:
            raise ProviderNotImplementedError(
                "Tiingo API key is not configured.",
                details={"provider": self.name, "setting": "TAPEHUB_TIINGO_API_KEY"},
            )
        return value

    # ------------------------------------------------------------------
    # WS Runtime 与容量
    # ------------------------------------------------------------------

    async def _ensure_runtime(self, api_key: str, asset_type: str) -> TiingoWsRuntime:
        """懒启动该资产类型端点的 WS Runtime（crypto/forex 各一实例）。

        ``start`` 幂等：连接任务已消亡（异常退出/事件循环关闭）时重启，
        ``_run`` 建连后会对全部活跃引用自动重订。
        """
        async with self._runtime_lock:
            runtime = self._runtimes.get(asset_type)
            if runtime is None:
                runtime = TiingoWsRuntime(
                    api_key=api_key,
                    asset_type=asset_type,
                    subscription_limit=self._subscription_limit(),
                )
                self._runtimes[asset_type] = runtime
            await runtime.start()
        return runtime

    def _ensure_capacity(self, runtime: TiingoWsRuntime | None, symbol: str) -> None:
        """容量闸门（急切）：该端点并发订阅 symbol 数达上限即拒绝。

        活跃口径为 Runtime 的 ``active_symbols``（含退订 linger 窗口）；
        Runtime 未启动时视为 0（首个订阅不受限）。存在并发受理的轻微
        竞态（软上限），越界由上游侧行为兜底。
        """
        if runtime is None:
            return
        active = runtime.active_symbols()
        if symbol not in active and len(active) >= self._subscription_limit():
            raise UpstreamSubscriptionLimitError(
                "Tiingo concurrent subscription limit reached, retry later.",
                details={
                    "provider": self.name,
                    "limit": self._subscription_limit(),
                    "active": active,
                },
            )

    async def _await_snapshot(
        self, runtime: TiingoWsRuntime, symbol: str
    ) -> dict[str, Any]:
        """等待首包快照；超时抛 ``QUOTE_NOT_READY``（不编造部分字段快照）。"""
        timeout = self._first_snapshot_timeout()
        try:
            return await runtime.wait_snapshot(symbol, timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise QuoteNotReadyError(
                "Quote snapshot not ready within the first-packet timeout.",
                details={
                    "provider": self.name,
                    "symbol": symbol,
                    "timeout_s": timeout,
                },
            ) from exc

    def _subscription_limit(self) -> int:
        return int(getattr(self.settings, "tiingo_subscription_limit", 8) or 8)

    def _first_snapshot_timeout(self) -> float:
        return float(getattr(self.settings, "tiingo_first_snapshot_timeout", 5.0) or 5.0)

    def _stream_queue_size(self) -> int:
        return int(getattr(self.settings, "stream_queue_size", 64) or 64)

    # ------------------------------------------------------------------
    # 历史 K 线（REST httpx2 直连，线程池）
    # ------------------------------------------------------------------

    def _fetch_sync(self, api_key: str, operation: str, query: BarQuery) -> BarBatch:
        rows = self._fetch_rows(api_key, operation, query)
        bars = to_standard_bars(rows, query.asset_type)
        # 上游按日期范围过滤，这里按闭区间 [start, end] 精确复核统一时间语义
        window = [bar for bar in bars if query.start <= bar.timestamp <= query.end]
        # fx PT1M 分段语义（定案见 docs/providers/tiingo.md）：单次请求仅
        # 返回窗口起点的第一个连续数据段——末根距请求 end 超容差即判定
        # 尾部截断（判定保留为防御，PT1M 当前不在声明周期集内）；
        # crypto 段上限按行数不限周期（行数上限官方未标注，不编造截断结论）
        partial = False
        if query.asset_type is AssetType.FOREX and query.interval == _FX_ONE_MIN and window:
            partial = (query.end - max(bar.timestamp for bar in window)) > _FX_SEGMENT_TOLERANCE
        elif query.asset_type is AssetType.CRYPTO and len(bars) >= _CRYPTO_MAX_ROWS:
            # crypto 段上限按行数（返回行数触顶即截断，方向保头截尾）
            partial = True
        page = paginate_bars(window, query.offset, query.limit)
        return BarBatch(
            instrument=query.instrument,
            interval=query.interval,
            bars=page,
            provider=self.name,
            received_at=datetime.now(CN_TIMEZONE),
            offset=query.offset,
            limit=query.limit,
            partial=partial,
        )

    def _fetch_rows(self, api_key: str, operation: str, query: BarQuery) -> list[dict[str, Any]]:
        """先完成参数映射，再延迟导入 httpx2 直连 REST（GET，Authorization 头）。"""
        url, params = build_request(operation, query)
        if operation == "fx_prices" and params["endDate"] < FX_ARCHIVE_FLOOR:
            # fx 完全越界窗口（endDate 亦早于档案起点）：直接空批，不发请求
            # （上游对该窗口返回 400，见 request_mapper 边界注释；startDate
            # 跨边界窗口已在 build_request 钳制，闭区间复核兜底）
            return []
        try:
            import httpx2
        except ImportError as exc:
            raise ProviderNotImplementedError(
                "Provider dependency 'httpx2' is not installed.",
                details={"provider": self.name, "sdk": "httpx2"},
            ) from exc
        headers = {"Authorization": f"Token {api_key}"}
        with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = client.get(url, params=params, headers=headers)
        if response.status_code == 429:
            # 免费档限频（50 请求/时、1000 请求/天）：明确映射可重试限流
            raise UpstreamRateLimitedError(
                "Tiingo upstream rate limit exceeded.",
                details={"provider": self.name, "status_code": response.status_code},
            )
        if response.status_code == 400 and UPSTREAM_NO_DATA_FINGERPRINT in response.text:
            # 请求区间早于上游档案起点（fx 端点硬边界 2020-01-01，报文见
            # request_mapper 指纹注释）——上游权威的无数据结论，
            # 归一化为空结果而非报错
            return []
        if response.status_code >= 400:
            raise UpstreamError(
                "Tiingo upstream returned an error response.",
                details={"provider": self.name, "status_code": response.status_code},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError(
                "Tiingo upstream returned a non-JSON response.",
                details={"provider": self.name, "status_code": response.status_code},
            ) from exc
        if not isinstance(payload, list):
            raise UpstreamError(
                "Tiingo upstream response is not a JSON array.",
                details={"provider": self.name, "status_code": response.status_code},
            )
        return [row for row in payload if isinstance(row, dict)]
