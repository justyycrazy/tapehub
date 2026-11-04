"""统一领域模型测试。"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, Bar, BarBatch, BarQuery, Instrument

UTC = timezone.utc
CN = ZoneInfo("Asia/Shanghai")


def _bar(ts: str, close: float | None = 1.0) -> Bar:
    return Bar(timestamp=datetime.fromisoformat(ts), close=close, open=None, high=None, low=None)


class TestInstrument:
    def test_parse_standard_code(self) -> None:
        instrument = Instrument.parse("CN-SSE-600000")
        assert instrument.market == "CN"
        assert instrument.exchange == "SSE"
        assert instrument.symbol == "600000"
        assert instrument.code == "CN-SSE-600000"

    @pytest.mark.parametrize(
        "raw",
        [
            "CN.SSE.600000",  # 点分隔不是标准格式
            "600000",
            "CN-SSE",
            "cn-sse-600000",  # 必须大写
            "",
        ],
    )
    def test_reject_invalid(self, raw: str) -> None:
        with pytest.raises(ValueError):
            Instrument.parse(raw)


class TestBar:
    def test_missing_values_stay_null(self) -> None:
        bar = Bar(
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            open=None, high=1.5, low=None, close=None,
        )
        dumped = bar.model_dump()
        assert dumped["open"] is None
        assert dumped["low"] is None
        assert dumped["close"] is None
        assert dumped["high"] == 1.5

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Bar(timestamp=datetime(2026, 1, 1))

    def test_timestamp_offset_preserved(self) -> None:
        # Bar 只要求时区感知，偏移原样保留；市场规范时区归一化由 BarBatch 完成
        bar = Bar(timestamp=datetime.fromisoformat("2026-01-01T08:00:00+08:00"))
        assert bar.timestamp.utcoffset() == timedelta(hours=8)
        assert bar.timestamp.hour == 8


class TestBarQuery:
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

    def test_defaults(self) -> None:
        query = self._query()
        assert query.provider == "auto"
        assert query.adjust == Adjust.NONE
        assert query.offset == 0
        assert query.limit == 500

    def test_start_must_be_earlier_than_end(self) -> None:
        with pytest.raises(ValidationError):
            self._query(
                start=datetime(2026, 1, 31, tzinfo=UTC), end=datetime(2026, 1, 1, tzinfo=UTC)
            )
        with pytest.raises(ValidationError):
            self._query(
                start=datetime(2026, 1, 1, tzinfo=UTC), end=datetime(2026, 1, 1, tzinfo=UTC)
            )

    def test_interval_from_string(self) -> None:
        query = self._query(interval="PT1M")
        assert query.interval == Interval.parse("PT1M")

    def test_bounds_normalized_to_market_zone(self) -> None:
        # CN 标的：任意合法偏移输入，边界统一归一化为北京时间
        query = self._query(
            start=datetime.fromisoformat("2026-01-01T08:00:00+08:00"),
            end=datetime.fromisoformat("2026-01-31T00:00:00Z"),
        )
        assert query.start.tzinfo == CN
        assert query.start.hour == 8
        assert query.end.tzinfo == CN
        assert query.end.hour == 8

    def test_global_bounds_normalized_to_beijing(self) -> None:
        # GLOBAL 标的（如 twelvedata 外汇）同样统一归一化为北京时间
        query = self._query(
            asset_type="forex",
            instrument="GLOBAL-OTC-EUR/USD",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 31, tzinfo=UTC),
        )
        assert query.start.tzinfo == CN
        assert query.start.hour == 8
        assert query.end.tzinfo == CN
        assert query.end.hour == 8

    def test_naive_bounds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._query(start=datetime(2026, 1, 1), end=datetime(2026, 1, 31, tzinfo=UTC))


class TestBarBatch:
    def test_count_is_actual_length(self) -> None:
        batch = BarBatch(
            instrument="CN-SSE-600000",
            interval="P1D",
            bars=[_bar("2026-01-01T00:00:00+00:00"), _bar("2026-01-02T00:00:00+00:00")],
            provider="fake-provider",
            received_at=datetime(2026, 2, 1, tzinfo=UTC),
            offset=0,
            limit=500,
        )
        assert batch.count == 2

    def test_global_batch_normalized_to_beijing(self) -> None:
        # GLOBAL 市场（twelvedata 外汇/加密货币）批次同样统一归一化为北京时间
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
