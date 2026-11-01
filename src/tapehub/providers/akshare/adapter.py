"""akshare 适配器骨架（未实现）。

接入约定：

- akshare 是可选依赖，禁止在模块顶层导入，真实调用只能在
  ``fetch_bars`` 内部延迟导入，并使用 ``call_in_thread`` 隔离同步阻塞；
- 参数转换与响应标准化分别收敛到本目录下的 request/response 映射代码；
- 接入前必须核实 akshare 的真实模块名与函数签名，不得臆造。
"""

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter


class ProviderAdapter(BaseProviderAdapter):
    """akshare 适配器占位实现，调用时返回 PROVIDER_NOT_IMPLEMENTED。"""

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        raise ProviderNotImplementedError(
            "Provider 'akshare' is not implemented yet.",
            details={"provider": self.name},
        )
