"""alphafeed 适配器骨架（未实现）。

SDK 包名、模块与函数尚未核实；接入时在本目录内补充
request/response 映射，并在 ``fetch_bars`` 内部延迟导入真实 SDK。
"""

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter


class ProviderAdapter(BaseProviderAdapter):
    """alphafeed 适配器占位实现，调用时返回 PROVIDER_NOT_IMPLEMENTED。"""

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        raise ProviderNotImplementedError(
            "Provider 'alphafeed' is not implemented yet.",
            details={"provider": self.name},
        )
