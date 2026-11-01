"""测试专用 Fake Provider。

返回确定性生成的受控测试数据，只能存在于测试环境；
用于验证统一请求 → 路由 → 内部 operation → 标准化 → 分页的完整链路。
"""

import zlib
from datetime import datetime, timedelta, timezone

from tapehub.domain.models import Bar, BarBatch, BarQuery
from tapehub.providers.base import BaseProviderAdapter
from tapehub.providers.pagination import paginate_bars

_BASE_PRICES: dict[str, float] = {
    "fake_stock_daily": 10.0,
    "fake_etf_daily": 5.0,
    "fake_index_daily": 3000.0,
    "fake_contract_daily": 4000.0,
    "fake_stock_minute": 12.5,
}

# 生成上限，防止超大时间范围拖垮测试。
_MAX_BARS = 100_000


class FakeBarProvider(BaseProviderAdapter):
    """演示两级路由的 Fake 适配器：manifest operation → 确定性数据生成。"""

    implemented = True

    async def fetch_bars(self, query: BarQuery) -> BarBatch:
        operation = self.resolve_operation(query)
        bars = self._generate(operation, query)
        page = paginate_bars(bars, query.offset, query.limit)
        return BarBatch(
            instrument=query.instrument,
            interval=query.interval,
            bars=page,
            provider=self.name,
            received_at=datetime.now(timezone.utc),
            offset=query.offset,
            limit=query.limit,
        )

    def _generate(self, operation: str, query: BarQuery) -> list[Bar]:
        step = timedelta(minutes=1) if query.interval.code == "PT1M" else timedelta(days=1)
        base = _BASE_PRICES.get(operation, 10.0)
        seed = zlib.crc32(query.instrument.code.encode("utf-8")) % 97
        bars: list[Bar] = []
        timestamp = query.start
        index = 0
        while timestamp <= query.end and index < _MAX_BARS:
            close = round(base + seed * 0.01 + (index % 7) * 0.05, 4)
            open_ = round(close - 0.02, 4)
            high = round(max(open_, close) + 0.03, 4)
            low = round(min(open_, close) - 0.03, 4)
            volume: float | None = float(1000 * (index % 5 + 1))
            amount: float | None = round(volume * close, 4)
            bars.append(
                Bar(
                    timestamp=timestamp,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=amount,
                    extensions=None,
                )
            )
            timestamp += step
            index += 1
        return bars
