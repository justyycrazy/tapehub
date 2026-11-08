"""标的模糊解析规则引擎的单元测试（纯函数，零网络、零 Provider）。

规则语义见 ``tapehub.domain.instrument_resolver`` 模块 docstring：
多候选如实返回（ambiguous），提示参数只做过滤，不验证标的真实存在。
"""

import pytest

from tapehub.domain.instrument_resolver import resolve_instrument
from tapehub.domain.models import AssetType


def codes(result: object) -> list[str]:
    """提取候选标准代码列表（断言辅助）。"""
    return [item.instrument.code for item in result.candidates]  # type: ignore[attr-defined]


def types(result: object) -> list[str | None]:
    """提取候选资产类型列表（断言辅助）。"""
    result_typed = result  # type: ignore[assignment]
    return [
        item.asset_type.value if item.asset_type else None
        for item in result_typed.candidates  # type: ignore[attr-defined]
    ]


class TestUserExamples:
    """需求验收的三个示例。"""

    def test_600000_is_sse_stock(self) -> None:
        result = resolve_instrument("600000")
        assert codes(result) == ["CN-SSE-600000"]
        assert types(result) == ["stock"]
        assert result.ambiguous is False

    def test_00700_is_hkex(self) -> None:
        result = resolve_instrument("00700")
        assert codes(result) == ["HK-HKEX-00700"]
        assert result.ambiguous is False

    def test_usdjpy_is_otc_forex(self) -> None:
        result = resolve_instrument("USDJPY")
        assert codes(result) == ["GLOBAL-OTC-USD/JPY"]
        assert types(result) == ["forex"]
        assert result.ambiguous is False


class TestCnSixDigit:
    """R2 纯 6 位数字：A 股代码段规则与 000 段双关。"""

    def test_sse_prefixes(self) -> None:
        for code in ("600000", "601398", "603259", "605111", "688981"):
            assert codes(resolve_instrument(code)) == [f"CN-SSE-{code}"]
            assert types(resolve_instrument(code)) == ["stock"]

    def test_sse_etf(self) -> None:
        assert codes(resolve_instrument("510300")) == ["CN-SSE-510300"]
        assert types(resolve_instrument("510300")) == ["etf"]

    def test_000001_ambiguous_index_and_stock(self) -> None:
        """0 段双关：上证指数 vs 平安银行，双候选不择一。"""
        result = resolve_instrument("000001")
        assert codes(result) == ["CN-SSE-000001", "CN-SZSE-000001"]
        assert types(result) == ["index", "stock"]
        assert result.ambiguous is True

    def test_000001_with_index_hint_narrows(self) -> None:
        result = resolve_instrument("000001", asset_type=AssetType.INDEX)
        assert codes(result) == ["CN-SSE-000001"]
        assert result.ambiguous is False

    def test_000001_with_stock_hint_narrows(self) -> None:
        result = resolve_instrument("000001", asset_type=AssetType.STOCK)
        assert codes(result) == ["CN-SZSE-000001"]

    def test_000001_with_etf_hint_empties(self) -> None:
        """提示过滤后可能为空：如实空候选，不回退、不猜测。"""
        assert codes(resolve_instrument("000001", asset_type=AssetType.ETF)) == []

    def test_szse_stock_prefixes(self) -> None:
        for code in ("300750", "301269", "002415"):
            assert codes(resolve_instrument(code)) == [f"CN-SZSE-{code}"]
            assert types(resolve_instrument(code)) == ["stock"]

    def test_szse_index_399(self) -> None:
        assert codes(resolve_instrument("399006")) == ["CN-SZSE-399006"]
        assert types(resolve_instrument("399006")) == ["index"]

    def test_bse_prefixes(self) -> None:
        for code in ("920047", "833533", "430047"):
            assert codes(resolve_instrument(code)) == [f"CN-BSE-{code}"]
            assert types(resolve_instrument(code)) == ["stock"]

    def test_unmapped_six_digit_empties(self) -> None:
        """无既定映射的 6 位数字（7/9 开头非 92）如实空候选。"""
        assert codes(resolve_instrument("700001")) == []
        assert codes(resolve_instrument("900001")) == []


class TestHkDigits:
    """R1 纯数字 1~5 位：港股零填充。"""

    def test_five_digit(self) -> None:
        assert codes(resolve_instrument("00700")) == ["HK-HKEX-00700"]

    def test_short_digits_zero_padded(self) -> None:
        assert codes(resolve_instrument("700")) == ["HK-HKEX-00700"]
        assert codes(resolve_instrument("2800")) == ["HK-HKEX-02800"]

    def test_asset_type_not_determined(self) -> None:
        """港股代码段不判资产类型（02800 是 ETF）：由调用方显式传参。"""
        assert types(resolve_instrument("00700")) == [None]


class TestUsTicker:
    """R5 纯字母/含点字母：美股 ticker 三候选。"""

    def test_ticker_three_exchange_candidates(self) -> None:
        result = resolve_instrument("AAPL")
        assert codes(result) == ["US-NYSE-AAPL", "US-NASDAQ-AAPL", "US-CBOE-AAPL"]
        assert result.ambiguous is True

    def test_exchange_hint_narrows(self) -> None:
        result = resolve_instrument("AAPL", exchange="nasdaq")
        assert codes(result) == ["US-NASDAQ-AAPL"]
        assert result.ambiguous is False

    def test_dotted_ticker(self) -> None:
        assert len(codes(resolve_instrument("BRK.B"))) == 3
        assert codes(resolve_instrument("BRK.B"))[0] == "US-NYSE-BRK.B"

    def test_lowercase_normalized(self) -> None:
        assert codes(resolve_instrument("aapl")) == [
            "US-NYSE-AAPL",
            "US-NASDAQ-AAPL",
            "US-CBOE-AAPL",
        ]

    def test_six_letters_not_ticker(self) -> None:
        """6 字母进入货币对规则而非 ticker 规则。"""
        assert codes(resolve_instrument("USDJPY")) == ["GLOBAL-OTC-USD/JPY"]


class TestForexPair:
    """R6 6 字母 3+3 双 ISO 4217 → 外汇。"""

    def test_major_pair(self) -> None:
        assert codes(resolve_instrument("EURUSD")) == ["GLOBAL-OTC-EUR/USD"]

    def test_lowercase_normalized(self) -> None:
        assert codes(resolve_instrument("usdjpy")) == ["GLOBAL-OTC-USD/JPY"]

    def test_order_preserved(self) -> None:
        """基准货币取前三位：USDJPY 与 JPYUSD 是不同对。"""
        assert codes(resolve_instrument("JPYUSD")) == ["GLOBAL-OTC-JPY/USD"]

    def test_non_iso_leg_not_forex(self) -> None:
        """非 ISO 货币码（BTC）不判外汇。"""
        assert codes(resolve_instrument("BTCUSD")) == ["GLOBAL-CRYPTO-BTC/USD"]

    def test_gibberish_empties(self) -> None:
        assert codes(resolve_instrument("FOOBAR")) == []


class TestCryptoPair:
    """R7 以 USD/USDT/USDC 结尾 → 加密货币。"""

    def test_usdt_quote_seven_letters(self) -> None:
        assert codes(resolve_instrument("BTCUSDT")) == ["GLOBAL-CRYPTO-BTC/USDT"]

    def test_usdc_quote(self) -> None:
        assert codes(resolve_instrument("ETHUSDC")) == ["GLOBAL-CRYPTO-ETH/USDC"]

    def test_doge_four_letter_base(self) -> None:
        assert codes(resolve_instrument("DOGEUSD")) == ["GLOBAL-CRYPTO-DOGE/USD"]


class TestStandardCodeEcho:
    """R0 已是标准代码：回显归一。"""

    def test_echo_uppercase(self) -> None:
        result = resolve_instrument("CN-SSE-600000")
        assert codes(result) == ["CN-SSE-600000"]
        assert types(result) == [None]

    def test_echo_normalizes_case(self) -> None:
        """三段大写归一（期货品种的交易所原生大小写由适配器还原）。"""
        assert codes(resolve_instrument("cn-sse-600000")) == ["CN-SSE-600000"]
        assert codes(resolve_instrument("CN-SHFE-cu2607")) == ["CN-SHFE-CU2607"]

    def test_echo_accepts_slash_symbol(self) -> None:
        assert codes(resolve_instrument("GLOBAL-OTC-EUR/USD")) == [
            "GLOBAL-OTC-EUR/USD"
        ]

    def test_hints_pass_through_echo(self) -> None:
        """类型未知（None）的候选不被 asset_type 提示过滤掉。"""
        result = resolve_instrument("CN-SSE-600000", asset_type=AssetType.STOCK)
        assert codes(result) == ["CN-SSE-600000"]


class TestPrefixSuffixStyles:
    """R3/R4 常见市场前后缀写法。"""

    def test_sh_prefix(self) -> None:
        result = resolve_instrument("sh600000")
        assert codes(result) == ["CN-SSE-600000"]
        assert types(result) == ["stock"]

    def test_sz_prefix_zero_segment_type_unknown(self) -> None:
        """前缀已定交易所，但 0 段类型不定 → 单候选类型 None。"""
        result = resolve_instrument("sz000001")
        assert codes(result) == ["CN-SZSE-000001"]
        assert types(result) == [None]

    def test_dot_suffix_sh(self) -> None:
        result = resolve_instrument("600000.SH")
        assert codes(result) == ["CN-SSE-600000"]
        assert types(result) == ["stock"]

    def test_dot_suffix_hk_zero_padded(self) -> None:
        assert codes(resolve_instrument("700.HK")) == ["HK-HKEX-00700"]

    def test_dot_suffix_us_three_candidates(self) -> None:
        result = resolve_instrument("AAPL.US")
        assert codes(result) == ["US-NYSE-AAPL", "US-NASDAQ-AAPL", "US-CBOE-AAPL"]

    def test_dot_suffix_bj(self) -> None:
        assert codes(resolve_instrument("920047.BJ")) == ["CN-BSE-920047"]


class TestFuturesAndInvalid:
    """期货不解析与非法输入的诚实边界。"""

    def test_futures_contract_empties(self) -> None:
        """期货品种与交易所无本地映射事实源：v1 不解析，空候选。"""
        assert codes(resolve_instrument("cu2607")) == []
        assert codes(resolve_instrument("RB2410")) == []

    def test_futures_standard_code_echoes(self) -> None:
        """已是标准代码的期货输入按 R0 回显。"""
        assert codes(resolve_instrument("CN-SHFE-RB")) == ["CN-SHFE-RB"]

    def test_mixed_alnum_empties(self) -> None:
        assert codes(resolve_instrument("ABC123")) == []

    def test_whitespace_trimmed(self) -> None:
        assert codes(resolve_instrument("  600000  ")) == ["CN-SSE-600000"]

    def test_exchange_hint_case_insensitive(self) -> None:
        assert codes(resolve_instrument("AAPL", exchange=" NASDAQ ")) == [
            "US-NASDAQ-AAPL"
        ]

    @pytest.mark.parametrize("bad", ["", " ", "-", "60000.5", "6000000"])
    def test_unmatchable_empties(self, bad: str) -> None:
        assert codes(resolve_instrument(bad)) == []
