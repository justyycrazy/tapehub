"""统一领域模型测试。"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, Bar, BarBatch, BarQuery, Instrument

UTC = timezone.utc


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

    def test_timestamp_converted_to_utc(self) -> None:
        bar = Bar(timestamp=datetime.fromisoformat("2026-01-01T08:00:00+08:00"))
        assert bar.timestamp.tzinfo == UTC
        assert bar.timestamp.hour == 0


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
