"""Provider 适配器基类与二级操作路由扩展机制。

公共 ProviderRouter 只负责“选择哪个 Provider”；
选中之后，由适配器根据 market × asset_type × interval × adjust
在本 manifest 的 operations 中完成第二次路由，并完成
参数转换（RequestMapper 职责）与响应标准化（ResponseMapper 职责）。

适配器不得依赖 FastAPI 请求上下文，未来后台 Worker 可直接复用。
"""

from collections.abc import Callable
from typing import Any

from tapehub.domain.capabilities import Capability
from tapehub.domain.errors import ProviderNotImplementedError, UnsupportedCapabilityError
from tapehub.domain.models import BarBatch, BarQuery, DataType
from tapehub.providers.manifest import ProviderManifest


class BaseProviderAdapter:
    """适配器基类：持有 manifest，提供内部 operation 解析。

    子类把 ``implemented`` 置为 True 才可参与路由；
    未实现的适配器保持禁用，或在被调用时抛出 ``ProviderNotImplementedError``。
    """

    implemented: bool = False

    def __init__(self, name: str, manifest: ProviderManifest) -> None:
        self.name = name
        self.manifest = manifest

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


async def call_in_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """把同步阻塞型 SDK 调用隔离到线程池，避免阻塞事件循环。"""
    import functools

    import anyio.to_thread

    return await anyio.to_thread.run_sync(functools.partial(func, *args, **kwargs))
