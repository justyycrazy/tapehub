"""标的模糊解析（instrument resolver）：宽松输入 → 标准代码候选。

纯本地规则引擎：零上游调用、零存储，只做「格式级推导」——不验证标的
真实存在（存在性由后续 bars/quotes 请求经上游验证）。多候选如实全部
返回，绝不静默择一；与主链路的边界：bars/quotes 仍只接受标准代码
（README「不接受 600000 等非标准格式」不变），本模块服务解析端点，
推荐「先解析、再查询」。

规则依据为公开市场代码约定（交易所代码段规则、ISO 4217 货币码表），
不代表任何上游数据源的能力声明：

- R0 已是标准代码：大写归一回显（三段统一大写，输入大小写不敏感；
  asset_type 无法由格式判定 → null）；
- R1 纯数字 1~5 位：港股，零填充到 5 位（asset_type 不判定）；
- R2 纯数字 6 位：A 股，交易所与资产类型由代码段推导——0 段为
  指数/个股双关（如 000001 同时是上证指数与平安银行），双候选；
- R3 市场后缀风格 ``代码.SH/SZ/BJ/HK/US``（如 600000.SH、AAPL.US）；
- R4 市场前缀风格 ``sh/sz/bj + 代码``（如 sh600000）；
- R5 纯字母 1~5 位或含点字母：美股 ticker（BRK.B 等）——ticker 推不出
  交易所，NYSE/NASDAQ/CBOE 三候选；
- R6 6 字母 3+3 且两段均为 ISO 4217：外汇 GLOBAL-OTC-基准/报价；
- R7 以 USD/USDT/USDC 结尾的 6~9 字母：加密货币 GLOBAL-CRYPTO-…；
- 其余输入（含期货/期权：品种代码与交易所无本地映射事实源）返回空候选。

候选 ``asset_type`` 只在输入格式能定型的场景给出（CN 代码段、外汇、
加密货币），否则为 null——查询时由调用方显式传参（asset_type 必填
不推断的原则不变）。
"""

import re
from dataclasses import dataclass

from tapehub.domain.models import AssetType, Instrument

# 美股 exchange 段白名单（与能力目录一致：OTC 粉单与 IEX 不接入）
_US_EXCHANGES: tuple[str, ...] = ("NYSE", "NASDAQ", "CBOE")

# ISO 4217 现行字母码（公开标准数据；新旧并存码并存收录，宽集合只影响
# 外汇判定的覆盖宽度，不构成任何上游能力声明）
_ISO_4217: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN
    BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD
    CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK
    DJF DKK DOP DZD
    EGP ERN ETB EUR
    FJD FKP
    GBP GEL GHS GIP GMD GNF GTQ GYD
    HKD HNL HRK HTG HUF
    IDR ILS INR IQD IRR ISK
    JMD JOD JPY
    KES KGS KHR KMF KPW KRW KWD KYD KZT
    LAK LBP LKR LRD LSL LYD
    MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN
    NAD NGN NIO NOK NPR NZD
    PAB PEN PGK PHP PKR PLN PYG
    QAR
    RON RSD RUB RWF
    SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL
    THB TJS TMT TND TOP TRY TTD TWD TZS
    UAH UGX USD USN UYI UYU UYW UZS
    VED VES VND VUV
    WST
    XAF XCD XDR XOF XPF XSU XTS XUA XAU XAG XPD XPT
    YER
    ZAR ZMW ZWL ZWG
    """.split()
)

# 加密货币常见报价币（R7 后缀）
_CRYPTO_QUOTES: tuple[str, ...] = ("USDT", "USDC", "USD")

# R3 市场后缀 → 标准代码 exchange 段（美股后缀不区分交易所）
_SUFFIX_EXCHANGES = {
    "SH": ("SSE",),
    "SZ": ("SZSE",),
    "BJ": ("BSE",),
    "HK": ("HKEX",),
    "US": _US_EXCHANGES,
}

# R4 市场前缀 → 标准代码 exchange 段
_PREFIX_EXCHANGES = {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}

_SUFFIX_RE = re.compile(
    r"^(?P<code>[0-9A-Za-z]+)\.(?P<sfx>SH|SZ|BJ|HK|US)$", re.IGNORECASE
)
_PREFIX_RE = re.compile(r"^(?P<pfx>sh|sz|bj)(?P<code>\d{1,6})$", re.IGNORECASE)
_US_TICKER_RE = re.compile(r"^(?:[A-Z]{1,5}|[A-Z]{1,4}\.[A-Z]{1,2})$")
_CRYPTO_RE = re.compile(r"^(?P<base>[A-Z]{2,5})(?P<quote>USDT|USDC|USD)$")


@dataclass(frozen=True)
class InstrumentCandidate:
    """单个解析候选：标准代码 + 可判定的资产类型（无法判定时为 None）。"""

    instrument: Instrument
    asset_type: AssetType | None


@dataclass(frozen=True)
class ResolveResult:
    """解析结果：规范化输入 + 候选列表（多于一个即歧义，由调用方收窄）。"""

    query: str
    candidates: tuple[InstrumentCandidate, ...]

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


def resolve_instrument(
    query: str,
    *,
    asset_type: AssetType | None = None,
    exchange: str | None = None,
) -> ResolveResult:
    """宽松输入解析为标准代码候选；提示参数（asset_type/exchange）收窄歧义。

    提示只做过滤：``asset_type`` 保留类型匹配或类型未知的候选，
    ``exchange`` 按标准代码 exchange 段精确匹配（大小写不敏感）。
    过滤后可能为空——如实返回空候选，不回退、不猜测。
    """
    text = query.strip()
    candidates = _match(text)
    if asset_type is not None:
        candidates = [
            item
            for item in candidates
            if item.asset_type is None or item.asset_type == asset_type
        ]
    if exchange is not None:
        wanted = exchange.strip().upper()
        candidates = [item for item in candidates if item.instrument.exchange == wanted]
    return ResolveResult(query=text, candidates=tuple(candidates))


def _match(text: str) -> list[InstrumentCandidate]:
    """按规则序派发；每条规则不适用返回 None，全部不命中返回空列表。"""
    for rule in (
        _rule_standard_code,
        _rule_suffix_style,
        _rule_prefix_style,
        _rule_cn_six_digit,
        _rule_hk_digits,
        _rule_us_ticker,
        _rule_forex_pair,
        _rule_crypto_pair,
    ):
        candidates = rule(text)
        if candidates is not None:
            return candidates
    return []


def _candidate(
    market: str, exchange: str, symbol: str, asset_type: AssetType | None
) -> InstrumentCandidate:
    return InstrumentCandidate(
        instrument=Instrument(market=market, exchange=exchange, symbol=symbol),
        asset_type=asset_type,
    )


def _rule_standard_code(text: str) -> list[InstrumentCandidate] | None:
    """R0 标准代码回显：三段大写归一（Instrument.parse 内统一处理）。"""
    try:
        return [InstrumentCandidate(instrument=Instrument.parse(text), asset_type=None)]
    except ValueError:
        return None


def _rule_suffix_style(text: str) -> list[InstrumentCandidate] | None:
    """R3 市场后缀风格：600000.SH → CN-SSE-600000、AAPL.US → 美股三候选。"""
    match = _SUFFIX_RE.match(text)
    if match is None:
        return None
    code = match.group("code")
    suffix = match.group("sfx").upper()
    if suffix in ("SH", "SZ", "BJ"):
        # A 股后缀已定交易所，资产类型按代码段推导（0 段类型不定 → None）
        exchange = _SUFFIX_EXCHANGES[suffix][0]
        asset_type = _cn_asset_type(code) if len(code) == 6 else None
        return [_candidate("CN", exchange, code, asset_type)]
    symbol = code.zfill(5) if suffix == "HK" and code.isdigit() else code.upper()
    return [
        _candidate(_market_of(exchange), exchange, symbol, None)
        for exchange in _SUFFIX_EXCHANGES[suffix]
    ]


def _rule_prefix_style(text: str) -> list[InstrumentCandidate] | None:
    """R4 市场前缀风格：sh600000 → CN-SSE-600000（前缀已定交易所）。"""
    match = _PREFIX_RE.match(text)
    if match is None:
        return None
    exchange = _PREFIX_EXCHANGES[match.group("pfx").lower()]
    code = match.group("code")
    asset_type = _cn_asset_type(code) if len(code) == 6 else None
    return [_candidate("CN", exchange, code, asset_type)]


def _rule_cn_six_digit(text: str) -> list[InstrumentCandidate] | None:
    """R2 纯 6 位数字 → A 股候选（交易所与资产类型由代码段推导）。"""
    if not (len(text) == 6 and text.isdigit()):
        return None
    return _cn_candidates(text)


def _rule_hk_digits(text: str) -> list[InstrumentCandidate] | None:
    """R1 纯数字 1~5 位 → 港股，零填充到 5 位（00700；asset_type 不判定）。"""
    if not (1 <= len(text) <= 5 and text.isdigit()):
        return None
    return [_candidate("HK", "HKEX", text.zfill(5), None)]


def _rule_us_ticker(text: str) -> list[InstrumentCandidate] | None:
    """R5 纯字母 1~5 位或含点字母 → 美股 ticker，交易所不可推导三候选。"""
    symbol = text.upper()
    if not _US_TICKER_RE.match(symbol):
        return None
    return [
        _candidate("US", exchange, symbol, None) for exchange in _US_EXCHANGES
    ]


def _rule_forex_pair(text: str) -> list[InstrumentCandidate] | None:
    """R6 6 字母 3+3 双 ISO 4217 → 外汇（前三位为基准货币）。"""
    if not (len(text) == 6 and text.isalpha()):
        return None
    upper = text.upper()
    base, quote = upper[:3], upper[3:]
    if base in _ISO_4217 and quote in _ISO_4217:
        return [_candidate("GLOBAL", "OTC", f"{base}/{quote}", AssetType.FOREX)]
    return None


def _rule_crypto_pair(text: str) -> list[InstrumentCandidate] | None:
    """R7 以 USD/USDT/USDC 结尾的 6~9 字母 → 加密货币聚合对。"""
    match = _CRYPTO_RE.match(text.upper())
    if match is None or len(text) < 6:
        return None
    base, quote = match.group("base"), match.group("quote")
    return [_candidate("GLOBAL", "CRYPTO", f"{base}/{quote}", AssetType.CRYPTO)]


def _market_of(exchange: str) -> str:
    """标准代码 market 段：美股 US、港股 HK（A 股后缀分支不经过此处）。"""
    return "HK" if exchange == "HKEX" else "US"


def _cn_candidates(code: str) -> list[InstrumentCandidate]:
    """纯 6 位数字 → A 股候选。

    代码段规则为公开交易所约定；0 段是指数/个股双关（上证指数系列与
    深市个股共用 000 段），双候选由调用方以 asset_type 提示收窄。
    """
    if code.startswith("399"):
        return [_candidate("CN", "SZSE", code, AssetType.INDEX)]
    if code.startswith("3"):
        return [_candidate("CN", "SZSE", code, AssetType.STOCK)]
    if code[0] == "6":
        return [_candidate("CN", "SSE", code, AssetType.STOCK)]
    if code[0] == "5":
        return [_candidate("CN", "SSE", code, AssetType.ETF)]
    if code.startswith("000"):
        # 000 段双关：上证指数系列（000016 等）与深市个股（000001 等）共用
        return [
            _candidate("CN", "SSE", code, AssetType.INDEX),
            _candidate("CN", "SZSE", code, AssetType.STOCK),
        ]
    if code[0] == "0":
        # 001/002/003 段为深市个股（中小板/主板），无指数双关
        return [_candidate("CN", "SZSE", code, AssetType.STOCK)]
    if code[0] in "48" or code.startswith("92"):
        return [_candidate("CN", "BSE", code, AssetType.STOCK)]
    return []


def _cn_asset_type(code: str) -> AssetType | None:
    """纯 6 位数字 → 代码段能定型的资产类型（交易所已由前后缀给定时使用）。"""
    if code.startswith("399"):
        return AssetType.INDEX
    if code.startswith("3") or code[0] in "46" or code.startswith("92"):
        return AssetType.STOCK
    if code[0] == "5":
        return AssetType.ETF
    return None


__all__ = [
    "InstrumentCandidate",
    "ResolveResult",
    "resolve_instrument",
]
