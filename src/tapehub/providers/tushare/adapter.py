"""tushare 适配器骨架（未实现）。

接入约定：

- tushare 是可选依赖，禁止在模块顶层导入，真实调用只能在
  ``fetch_bars`` 内部延迟导入，并使用 ``call_in_thread`` 隔离同步阻塞；
- Token 只能通过环境变量提供，禁止写入 manifest、日志或异常响应；
- 接入前必须核实 tushare 的真实接口名与参数，不得臆造。
"""

from tapehub.domain.errors import ProviderNotImplementedError
from tapehub.domain.models import BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter


class ProviderAdapter(BaseProviderAdapter):
    """tushare 适配器占位实现，调用时返回 PROVIDER_NOT_IMPLEMENTED。"""

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        raise ProviderNotImplementedError(
            "Provider 'tushare' is not implemented yet.",
            details={"provider": self.name},
        )
