"""领域模型归一化的非北京偏移守护测试。

领域层输入边界只要求时区感知，全市场统一归一化为北京时间（+08:00）——
本文件刻意以 UTC / 混合偏移输入验证该归一化路径，防止将来有人收窄
输入约束或破坏归一化；常规行为测试见 ``test_models.py``（北京时间桩）。
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tapehub.domain.models import Bar, BarBatch, BarQuery

UTC = timezone.utc
CN = ZoneInfo("Asia/Shanghai")


def _bar(ts: str, close: float | None = 1.0) -> Bar:
    return Bar(timestamp=datetime.fromisoformat(ts), close=close, open=None, high=None, low=None)


class TestBarQueryUtcNormalization:
    def _query(self, **overrides: object) -> BarQuery:
        base: dict[str, object] = {
            "asset_type": "stock",
            "instrument": "CN-SSE-600000",
            "interval": "P1D",
            "start": datetime(2026, 1, 1, tzinfo=UTC),
            "end": datetime(2026, 1, 31, tzinfo=UTC),
        }
        base.update(overrides)
        return BarQuery(**base)  # type: ignore[arg-type]

    def test_mixed_offset_bounds_normalized_to_beijing(self) -> None:
        # 混合偏移输入（+08:00 与 Z）：边界统一归一化为北京时间
        query = self._query(
            start=datetime.fromisoformat("2026-01-01T08:00:00+08:00"),
            end=datetime.fromisoformat("2026-01-31T00:00:00Z"),
        )
        assert query.start.tzinfo == CN
        assert query.start.hour == 8
        assert query.end.tzinfo == CN
        assert query.end.hour == 8

    def test_utc_bounds_normalized_to_beijing(self) -> None:
        # GLOBAL 标的（如 twelvedata 外汇）UTC 输入同样统一归一化为北京时间
        query = self._query(
            asset_type="forex",
            instrument="GLOBAL-OTC-EUR/USD",
        )
        assert query.start.tzinfo == CN
        assert query.start.hour == 8
        assert query.end.tzinfo == CN
        assert query.end.hour == 8


class TestBarBatchUtcNormalization:
    def test_utc_batch_normalized_to_beijing(self) -> None:
        # GLOBAL 市场（twelvedata 外汇/加密货币）UTC 时间字段同样统一归一化为北京时间
        batch = BarBatch(
            instrument="GLOBAL-CRYPTO-BTC/USD",
            interval="P1D",
            bars=[_bar("2026-01-01T00:00:00+00:00"), _bar("2026-01-02T00:00:00+00:00")],
            provider="fake-provider",
            received_at=datetime(2026, 2, 1, tzinfo=UTC),
            offset=0,
            limit=500,
        )
        assert batch.bars[0].timestamp == datetime.fromisoformat("2026-01-01T08:00:00+08:00")
        assert batch.bars[1].timestamp == datetime.fromisoformat("2026-01-02T08:00:00+08:00")
        assert batch.received_at == datetime.fromisoformat("2026-02-01T08:00:00+08:00")
