"""标的解析服务：宽松输入 → tapehub 标准代码候选。

规则引擎在 Domain 层（``instrument_resolver``，纯本地规则、零上游调用）；
本服务只做层间编排，不引入任何 Provider 依赖，不验证标的真实存在。
"""

from tapehub.domain.instrument_resolver import ResolveResult, resolve_instrument
from tapehub.domain.models import AssetType


class InstrumentResolveService:
    """无状态解析服务：规则为纯函数，实例仅为组合根装配统一。"""

    def resolve(
        self,
        query: str,
        *,
        asset_type: AssetType | None = None,
        exchange: str | None = None,
    ) -> ResolveResult:
        """解析宽松输入；asset_type/exchange 提示用于收窄歧义候选。"""
        return resolve_instrument(query, asset_type=asset_type, exchange=exchange)
