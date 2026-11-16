"""统一领域模型测试。

桩数据统一北京时间（+08:00）；「非北京偏移输入 → 北京时间归一化」的
守护维度由 ``test_models_utc.py`` 以 UTC / 混合偏移输入单独验证。
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, Bar, BarBatch, BarQuery, Instrument, QuoteQuery

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

    def test_parse_normalizes_case(self) -> None:
        """输入大小写不敏感，三段统一大写归一（期货品种随交易所由适配器还原）。"""
        instrument = Instrument.parse("cn-sse-cu2607")
        assert instrument.code == "CN-SSE-CU2607"
        assert Instrument.parse("CN-SHFE-CU2607").symbol == "CU2607"

    def test_field_construction_normalizes_case(self) -> None:
        """字段直构同样大写归一，保证不变式：Instrument 三段恒为规范大写。"""
        instrument = Instrument(market="cn", exchange="shfe", symbol="cu2607")
        assert instrument.code == "CN-SHFE-CU2607"

    @pytest.mark.parametrize(
        "raw",
        [
            "CN.SSE.600000",  # 点分隔不是标准格式
            "600000",
            "CN-SSE",
            "",
        ],
    )
    def test_reject_invalid(self, raw: str) -> None:
        with pytest.raises(ValueError):
            Instrument.parse(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "CN-SSE-600000",
            "CN-SHFE-CU2607",  # 期货交易所段
            "CN-CFFEX-IO2601-C-4000",  # 中金所期权
            "CN-CSI-000905",  # 中证指数段
            "HK-HKEX-00700",
            "US-NYSE-SPY",
            "US-OTC-PINK",  # OTC/IEX 属合法词汇，接入与否由 Provider 收窄
            "GLOBAL-OTC-EUR/USD",
            "GLOBAL-CRYPTO-BTCUSD",
        ],
    )
    def test_vocabulary_valid_combinations(self, raw: str) -> None:
        """market/exchange 组合在标准代码词汇表内：正常构造。"""
        assert Instrument.parse(raw).code == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "CN-NYSE-SPY",  # NYSE 不属 CN 词汇
            "US-SSE-600000",  # SSE 不属 US 词汇
            "HK-NYSE-00700",
            "CN-HKEX-00700",
            "GLOBAL-NYSE-EUR/USD",
            "XX-FOO-BAR",  # market 段不在词汇表
        ],
    )
    def test_reject_contradictory_market_exchange(self, raw: str) -> None:
        """market/exchange 矛盾组合构造期即拒绝，不透传映射层。"""
        with pytest.raises(ValueError, match="is not a valid"):
            Instrument.parse(raw)


class TestBar:
    def test_missing_values_stay_null(self) -> None:
        bar = Bar(
            timestamp=datetime(2026, 1, 1, tzinfo=CN),
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
            "start": datetime(2026, 1, 1, tzinfo=CN),
            "end": datetime(2026, 1, 31, tzinfo=CN),
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
                start=datetime(2026, 1, 31, tzinfo=CN), end=datetime(2026, 1, 1, tzinfo=CN)
            )
        with pytest.raises(ValidationError):
            self._query(
                start=datetime(2026, 1, 1, tzinfo=CN), end=datetime(2026, 1, 1, tzinfo=CN)
            )

    def test_interval_from_string(self) -> None:
        query = self._query(interval="PT1M")
        assert query.interval == Interval.parse("PT1M")

    def test_naive_bounds_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._query(start=datetime(2026, 1, 1), end=datetime(2026, 1, 31, tzinfo=CN))

    @pytest.mark.parametrize(
        ("asset_type", "instrument"),
        [
            ("commodity-option", "CN-SHFE-CU2701C102000"),
            ("commodity-option", "CN-CZCE-SR2701C5900"),
            ("index-option", "CN-CFFEX-IO2612C4000"),
            ("etf-option", "CN-SSE-10012437"),
            ("etf-option", "CN-SZSE-90008083"),
        ],
    )
    def test_option_subtype_valid_combinations(self, asset_type: str, instrument: str) -> None:
        """期权子类 × 本所组合（CN 期权八所归属，探针实证）：正常构造。"""
        query = self._query(asset_type=asset_type, instrument=instrument)
        assert query.asset_type.value == asset_type

    @pytest.mark.parametrize(
        ("asset_type", "instrument"),
        [
            ("commodity-option", "CN-SHFE-CU2701C102000"),  # SHFE/INE 无连字符直通式
            ("commodity-option", "CN-DCE-M2701C3500"),  # 连字符为映射层还原，标准式无
            ("commodity-option", "CN-CZCE-SR2701C5900"),  # CZCE 统一 4 位年月
            ("index-option", "CN-CFFEX-IO2612P4000"),  # P 方向与 C 同构
            ("index-option", "CN-CFFEX-IO2612C4000.5"),  # 行权价小数未观测但放行
        ],
    )
    def test_option_unified_symbol_valid(self, asset_type: str, instrument: str) -> None:
        """结构子类统一符号式 {品种}{YYMM}{C/P}{行权价}：正常构造。"""
        query = self._query(asset_type=asset_type, instrument=instrument)
        assert query.instrument.code == instrument

    def test_option_unified_symbol_lowercase_normalized(self) -> None:
        """小写统一式输入经域层大写归一后通过校验（无单独小写通道）。"""
        query = self._query(
            asset_type="commodity-option", instrument="cn-shfe-cu2701c102000"
        )
        assert query.instrument.code == "CN-SHFE-CU2701C102000"

    @pytest.mark.parametrize(
        ("asset_type", "instrument"),
        [
            ("commodity-option", "CN-DCE-M2701-C-3500"),  # 旧连字符式
            ("commodity-option", "CN-CZCE-SR701C5900"),  # 旧 3 位月码
            ("commodity-option", "CN-GFEX-LC2701-C-90000"),  # 旧连字符式
            ("index-option", "CN-CFFEX-IO2612-C-4000"),  # 旧连字符式
            ("commodity-option", "CN-SHFE-CU2701C10X000"),  # 非法行权价
            ("commodity-option", "CN-SHFE-CU2701C"),  # 缺行权价
            ("commodity-option", "CN-SHFE-CU27C102000"),  # 年月非 4 位
        ],
    )
    def test_option_unified_symbol_invalid_rejected(
        self, asset_type: str, instrument: str
    ) -> None:
        """结构子类非统一式（旧写法/残缺形态）构造期拒绝，不透传映射层。"""
        with pytest.raises(ValidationError, match="requires instrument symbol"):
            self._query(asset_type=asset_type, instrument=instrument)

    @pytest.mark.parametrize(
        ("asset_type", "instrument"),
        [
            ("commodity-option", "CN-SSE-10012437"),  # SSE 属 etf-option
            ("commodity-option", "CN-CFFEX-IO2612C4000"),  # CFFEX 属 index-option
            ("index-option", "CN-SHFE-CU2701C102000"),  # SHFE 属 commodity-option
            ("etf-option", "CN-CFFEX-IO2612C4000"),  # CFFEX 属 index-option
            ("etf-option", "CN-DCE-M2701C3500"),  # DCE 属 commodity-option
            # BSE（北交所）无期权，三子类词汇表均不含
            ("commodity-option", "CN-BSE-920001"),
            ("index-option", "CN-BSE-920001"),
            ("etf-option", "CN-BSE-920001"),
        ],
    )
    def test_option_subtype_contradictory_exchange_rejected(
        self, asset_type: str, instrument: str
    ) -> None:
        """期权子类 × 交易所矛盾组合构造期拒绝（词汇表校验），不透传映射层。"""
        with pytest.raises(ValidationError, match="does not apply to exchange"):
            self._query(asset_type=asset_type, instrument=instrument)


class TestQuoteQuery:
    @pytest.mark.parametrize(
        ("asset_type", "instrument"),
        [
            ("commodity-option", "CN-INE-SC2701C550"),
            ("index-option", "CN-CFFEX-IO2612C4000"),
            ("etf-option", "CN-SZSE-90008083"),
        ],
    )
    def test_option_subtype_valid_combinations(self, asset_type: str, instrument: str) -> None:
        """快照查询同受期权子类×交易所词汇表与统一式约束：合法组合正常构造。"""
        query = QuoteQuery(asset_type=asset_type, instrument=instrument)
        assert query.asset_type.value == asset_type

    def test_option_subtype_contradictory_exchange_rejected(self) -> None:
        """快照查询矛盾组合（commodity-option + CN-SSE）构造期拒绝。"""
        with pytest.raises(ValidationError, match="does not apply to exchange"):
            QuoteQuery(asset_type="commodity-option", instrument="CN-SSE-10012437")


class TestBarBatch:
    def test_count_is_actual_length(self) -> None:
        batch = BarBatch(
            instrument="CN-SSE-600000",
            interval="P1D",
            bars=[_bar("2026-01-01T00:00:00+08:00"), _bar("2026-01-02T00:00:00+08:00")],
            provider="fake-provider",
            received_at=datetime(2026, 2, 1, tzinfo=CN),
            offset=0,
            limit=500,
        )
        assert batch.count == 2
