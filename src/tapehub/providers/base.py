"""Provider 适配器基类与二级操作路由扩展机制。

公共 ProviderRouter 只负责“选择哪个 Provider”；
选中之后，由适配器根据 market × asset_type × interval × adjust
在本 manifest 的 operations 中完成第二次路由，并完成
参数转换（RequestMapper 职责）与响应标准化（ResponseMapper 职责）。

适配器不得依赖 FastAPI 请求上下文，未来后台 Worker 可直接复用。
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tapehub.domain.capabilities import Capability
from tapehub.domain.errors import (
    ConcurrencyLimitError,
    ProviderNotImplementedError,
    UnsupportedCapabilityError,
)
from tapehub.domain.models import BarBatch, BarQuery, DataType, Quote, QuoteQuery
from tapehub.providers.manifest import ProviderManifest

if TYPE_CHECKING:
    from anyio import CapacityLimiter

    from tapehub.settings import Settings


# 分组线程池的统一默认容量：thread_pool_size_map 开启分组后，未显式配置的
# 内置 Provider 用该值（10 家合计 40，恰等于全局默认 thread_pool_size——
# 开启分组默认不改变总在飞预算，只改变分配方式）
DEFAULT_THREAD_POOL_CAPACITY = 4


def resolve_thread_capacity(
    name: str, settings: "Settings | None"
) -> "CapacityLimiter | None":
    """按 Provider 名解析分组线程池容量；分组未开启返回 None。

    ``Settings.thread_pool_size_map`` 非空即分组开启：map 列出的 Provider
    用配置容量（≥1，pydantic 启动期校验），未列出的用统一默认容量
    （``DEFAULT_THREAD_POOL_CAPACITY``）。返回 None 表示分组未开启，
    ``call_in_thread`` 沿用 anyio 全局默认 limiter（与分组机制引入前的
    行为完全一致）。

    ``CapacityLimiter`` 是普通对象（anyio 实测可在事件循环外构造），
    适配器在注册表构建期为进程内单例，实例级持有即覆盖进程生命周期。
    """
    size_map = getattr(settings, "thread_pool_size_map", None) or {}
    if not size_map:
        return None
    capacity = int(size_map.get(name, DEFAULT_THREAD_POOL_CAPACITY))
    import anyio

    return anyio.CapacityLimiter(capacity)


class BaseProviderAdapter:
    """适配器基类：持有 manifest 与服务配置，提供内部 operation 解析。

    子类把 ``implemented`` 置为 True 才可参与路由；
    未实现的适配器保持禁用，或在被调用时抛出 ``ProviderNotImplementedError``。
    需要凭据的 Provider 从 ``settings`` 读取（环境变量注入），不得写入代码或日志。
    """

    implemented: bool = False

    def __init__(
        self,
        name: str,
        manifest: ProviderManifest,
        settings: "Settings | None" = None,
    ) -> None:
        self.name = name
        self.manifest = manifest
        self.settings = settings
        # 分组线程池 limiter：适配器为进程内单例，实例级持有覆盖生命周期
        # （与实例级共享 httpx 客户端同构）；None = 分组未开启（全局默认池）
        self._thread_limiter = resolve_thread_capacity(name, settings)

    @property
    def thread_limiter(self) -> "CapacityLimiter | None":
        """本 Provider 的分组线程池 limiter（只读暴露：可观测性与测试挂钩）。

        非 None 时可读 ``total_tokens``（容量）与 ``borrowed_tokens``（在飞
        占用）；None 表示分组未开启（全部线程型调用共享 anyio 全局默认池）。
        """
        return self._thread_limiter

    def call_in_thread(
        self,
        func: Callable[..., Any],
        /,
        *args: Any,
        abandon_on_cancel: bool = False,
        **kwargs: Any,
    ) -> Any:
        """以本 Provider 的分组线程池执行同步调用（模块级 ``call_in_thread``
        的实例方法包装）。

        分组未开启（limiter 为 None）：行为与模块级函数完全一致（anyio
        全局默认池）。分组开启：
        - 名额已满：同步抛 ``ConcurrencyLimitError``（HTTP 429，可重试，
          details 携带 provider 与 limit）——快速背压，不无限排队；
        - 名额未满：经自家 limiter 进入线程池，容量独享、与他家互不影响
          （单家上游挂死的名额压力被隔离在本家池内）。
        check-then-act 存在无害竞态：检查与占用间隙中的超额调用进入
        run_sync 内部 acquire 排队（不产生超额线程），由应用层
        upstream_timeout_seconds 兜底为 UPSTREAM_TIMEOUT（504）。
        """
        limiter = self._thread_limiter
        if limiter is not None and limiter.borrowed_tokens >= limiter.total_tokens:
            raise ConcurrencyLimitError(
                "Provider thread pool capacity reached, retry later.",
                details={"provider": self.name, "limit": limiter.total_tokens},
            )
        return call_in_thread(
            func,
            *args,
            abandon_on_cancel=abandon_on_cancel,
            limiter=limiter,
            **kwargs,
        )

    @property
    def capabilities(self) -> list[Capability]:
        return self.manifest.to_domain_capabilities()

    def resolve_operation(self, query: BarQuery) -> str:
        """内部二级路由：按 (market, asset_type, interval, adjust, variant) 匹配 operation。

        相同 instrument/provider/interval 在不同 asset_type 或 variant 下会命中
        不同 operation，不会复用错误的上游操作；
        无匹配时返回 ``UNSUPPORTED_CAPABILITY``。
        """
        for op in self.manifest.operations:
            m = op.match
            if (
                m.data_type == DataType.BARS
                and m.market == query.instrument.market
                and m.asset_type == query.asset_type
                and m.interval == query.interval
                and m.adjust == query.adjust
                and m.variant == query.variant
            ):
                return op.operation
        raise UnsupportedCapabilityError(
            "No internal operation matches the requested capability.",
            details={
                "provider": self.name,
                "market": query.instrument.market,
                "asset_type": query.asset_type.value,
                "interval": query.interval.code,
                "adjust": query.adjust.value,
                "variant": query.variant.value,
            },
        )

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        raise ProviderNotImplementedError(
            f"Provider {self.name!r} is not implemented yet.",
            details={"provider": self.name},
        )

    def resolve_quote_operation(self, query: QuoteQuery) -> str:
        """快照链路的内部二级路由：(market, asset_type, variant) × data_type=quotes。

        quote 无周期/复权维度（manifest 中显式声明 null），其余语义同
        ``resolve_operation``：无匹配时抛出 ``UNSUPPORTED_CAPABILITY``。
        """
        for op in self.manifest.operations:
            m = op.match
            if (
                m.data_type == DataType.QUOTE
                and m.market == query.instrument.market
                and m.asset_type == query.asset_type
                and m.variant == query.variant
            ):
                return op.operation
        raise UnsupportedCapabilityError(
            "No internal operation matches the requested capability.",
            details={
                "provider": self.name,
                "market": query.instrument.market,
                "asset_type": query.asset_type.value,
                "variant": query.variant.value,
            },
        )

    async def fetch_quote(self, query: QuoteQuery) -> Quote:
        raise ProviderNotImplementedError(
            f"Provider {self.name!r} is not implemented yet.",
            details={"provider": self.name},
        )


async def call_in_thread(
    func: Callable[..., Any],
    /,
    *args: Any,
    abandon_on_cancel: bool = False,
    limiter: "CapacityLimiter | None" = None,
    **kwargs: Any,
) -> Any:
    """把同步阻塞型 SDK 调用隔离到线程池，避免阻塞事件循环。

    ``limiter``：显式 ``CapacityLimiter``（Provider 分组线程池，见
    ``BaseProviderAdapter.call_in_thread``）；None 用 anyio 全局默认
    limiter（历史行为）。名额占用语义：无论 abandon 分支，limiter 名额
    都随调用方取消即时释放（run_sync 以 ``async with limiter`` 包裹执行），
    被弃置的工作线程继续在后台跑完但不占用名额。

    ``abandon_on_cancel=False``（默认，shield）：调用方取消延迟到线程内调用
    完成后才送达——适用于必须完整执行的启动类操作（如 tqsdk runtime 启动，
    取消残留由调用方在 except 路径收编）。
    ``abandon_on_cancel=True``（可放弃）：取消立即送达调用方（线程在后台
    跑完当前上游调用、结果丢弃）——适用于数据取数：请求级超时（应用层
    wait_for → 504）与客户端断开即时生效，不等 SDK 自然结束。线程不被
    强杀，SDK 调用按自身节奏完成（baostock/akshare 等上游 socket 无超时
    属 SDK 限制，最坏留下一个后台孤儿线程至调用自然结束，但不占用名额）。
    """
    import functools

    import anyio.to_thread

    return await anyio.to_thread.run_sync(
        functools.partial(func, *args, **kwargs),
        abandon_on_cancel=abandon_on_cancel,
        limiter=limiter,
    )


def truncate_upstream_message(value: object, limit: int = 200) -> str:
    """上游报文文本进错误 details 前的统一截断（信息泄露纵深）。

    上游业务报文由上游控制，理论上可能回显请求参数或账户信息；错误响应
    details 只保留截断文本，全文经调用方 ``logger.warning`` 留在服务端
    日志（可按 request_id 关联）。
    """
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"
