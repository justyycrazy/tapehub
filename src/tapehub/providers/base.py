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
from tapehub.domain.errors import ProviderNotImplementedError, UnsupportedCapabilityError
from tapehub.domain.models import BarBatch, BarQuery, DataType, Quote, QuoteQuery
from tapehub.providers.manifest import ProviderManifest

if TYPE_CHECKING:
    from tapehub.settings import Settings


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
    **kwargs: Any,
) -> Any:
    """把同步阻塞型 SDK 调用隔离到线程池，避免阻塞事件循环。

    ``abandon_on_cancel=False``（默认，shield）：调用方取消延迟到线程内调用
    完成后才送达——适用于必须完整执行的启动类操作（如 tqsdk runtime 启动，
    取消残留由调用方在 except 路径收编）。
    ``abandon_on_cancel=True``（可放弃）：取消立即送达调用方（线程在后台
    跑完当前上游调用、结果丢弃）——适用于数据取数：请求级超时（应用层
    wait_for → 504）与客户端断开即时生效，不等 SDK 自然结束。线程不被
    强杀，SDK 调用按自身节奏完成（baostock/akshare 等上游 socket 无超时
    属 SDK 限制，最坏占用一个 worker 线程至调用自然结束）。
    """
    import functools

    import anyio.to_thread

    return await anyio.to_thread.run_sync(
        functools.partial(func, *args, **kwargs),
        abandon_on_cancel=abandon_on_cancel,
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
