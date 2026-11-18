"""futunn 适配器单元测试：签名、请求映射、响应映射与运行前提（HTTP 全部 Mock）。

签名走真实 cryptography 路径（测试私钥现场生成，公钥验签断言），HTTP 经
stub_futunn 桩注入，无真实网络。
"""

import asyncio
import base64
import hashlib
import sys
import urllib.parse
from datetime import datetime
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    UnsupportedCapabilityError,
    UpstreamError,
    UpstreamRateLimitedError,
)
from tapehub.domain.intervals import Interval
from tapehub.domain.models import (
    CN_TIMEZONE,
    Adjust,
    AssetType,
    BarQuery,
    DataVariant,
    Instrument,
    QuoteQuery,
)
from tapehub.providers.futunn.request_mapper import REST_BASE
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tests.conftest import make_settings
from tests.fixtures.providers.stub_futunn import (
    StubClient,
    envelope,
    install,
    kline_page,
    preset_pages,
    preset_payload,
    preset_status,
    quote_payload,
    quote_row,
    write_test_ed25519_key,
    write_test_rsa_key,
)

# 北京零点毫秒戳（Asia/Shanghai）：2026-01-01 / 01-02 / 01-10
DAY1_MS = 1767196800000
DAY2_MS = 1767283200000
DAY10_MS = 1767974400000
_DAY_MS = 86_400_000
# 快照行情时间：2026-01-01T15:00+08:00（A 股收盘后）
QUOTE_TIME_MS = 1767250800000


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        "end": datetime(2026, 1, 10, tzinfo=CN_TIMEZONE),
        "adjust": Adjust.NONE,
        "variant": DataVariant.FUTUNN,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _quote_query(**overrides: Any) -> QuoteQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "variant": DataVariant.FUTUNN,
    }
    base.update(overrides)
    return QuoteQuery(**base)


def _adapter(settings: Any = None) -> Any:
    from tapehub.providers.futunn.adapter import ProviderAdapter

    manifest = load_builtin_manifest("futunn")
    record = build_provider_record("futunn", manifest, ProviderAdapter, settings=settings)
    return record.adapter


def _last_call() -> dict[str, Any]:
    return StubClient.instances[-1].calls[-1]


def _signature_message(call: dict[str, Any]) -> bytes:
    """从桩记录还原签名原文（timestamp\\nMETHOD\\npath\\nquery\\nbody_sha256）。"""
    parsed = urllib.parse.urlsplit(call["url"])
    body = call["content"]
    body_part = hashlib.sha256(body).hexdigest() if body else ""
    headers = call["headers"]
    return "\n".join(
        [headers["X-Timestamp"], call["method"], parsed.path, parsed.query, body_part]
    ).encode("utf-8")


@pytest.fixture(scope="module")
def rsa_key(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Any]:
    return write_test_rsa_key(tmp_path_factory.mktemp("futunn-keys"))


@pytest.fixture(scope="module")
def ed25519_key(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Any]:
    return write_test_ed25519_key(tmp_path_factory.mktemp("futunn-keys"))


@pytest.fixture()
def futunn_settings(rsa_key: tuple[str, Any]) -> Any:
    return make_settings(
        futunn_app_key="test-app-key", futunn_private_key_path=rsa_key[0]
    )


# ---------------------------------------------------------------------------
# 请求映射与签名请求头
# ---------------------------------------------------------------------------


def test_stock_bars_request_mapping_and_signed_headers(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS, DAY2_MS]))
    adapter = _adapter(futunn_settings)

    batch = asyncio.run(adapter.fetch_bars(_query()))

    call = _last_call()
    assert call["method"] == "GET"
    # 端点按上游前缀代码；查询串固定键序（start/end/ktype/autype/num），
    # 无 page_token（单页即止）；autype/num 显式传参不依赖上游默认值
    assert call["url"] == (
        f"{REST_BASE}/api/v1.0/quote/SH.600000/history-kline"
        "?start=2026-01-01&end=2026-01-10&ktype=2&autype=0&num=370"
    )
    headers = call["headers"]
    assert headers["X-Api-Key"] == "test-app-key"
    assert headers["Authorization"]
    assert not headers["Authorization"].startswith("Bearer")  # 无 Bearer 前缀
    assert len(headers["X-Timestamp"]) == 13  # 毫秒时间戳
    assert 1 <= len(headers["X-Nonce"]) <= 64
    assert headers["Content-Type"] == "application/json"

    assert batch.provider == "futunn"
    assert batch.partial is False
    assert batch.count == 2
    # 北京零点毫秒戳 → 统一北京时间；行值按行序递增造数（见 stub kline_rows）
    assert batch.bars[0].timestamp == datetime(2026, 1, 1, 0, tzinfo=CN_TIMEZONE)
    assert (batch.bars[0].open, batch.bars[0].close) == (10.0, 10.5)
    assert batch.bars[0].volume == 1000.0  # 股（volume_precision 缺省 0 不缩放）
    assert batch.bars[0].amount == 10500.0  # turnover → amount


def test_signature_rsa_verifies(
    rsa_key: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AppKey 签名（RSA-SHA256 PKCS#1 v1.5）：签名原文还原后公钥验签通过，
    篡改原文验签失败。"""
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(
        make_settings(futunn_app_key="test-app-key", futunn_private_key_path=rsa_key[0])
    )
    asyncio.run(adapter.fetch_bars(_query()))

    call = _last_call()
    message = _signature_message(call)
    signature = base64.b64decode(call["headers"]["Authorization"])
    rsa_key[1].verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(InvalidSignature):
        rsa_key[1].verify(signature, message + b"x", padding.PKCS1v15(), hashes.SHA256())


def test_signature_ed25519_verifies(
    ed25519_key: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AppKey 签名（Ed25519，算法按私钥类型自动检测）：POST 快照路径覆盖
    body_part（SHA256 hex）签名原文段。"""
    install(monkeypatch)
    preset_payload(monkeypatch, quote_payload([quote_row("SH.600000", QUOTE_TIME_MS)]))
    adapter = _adapter(
        make_settings(
            futunn_app_key="test-app-key", futunn_private_key_path=ed25519_key[0]
        )
    )
    asyncio.run(adapter.fetch_quote(_quote_query()))

    call = _last_call()
    assert call["method"] == "POST"
    message = _signature_message(call)
    signature = base64.b64decode(call["headers"]["Authorization"])
    ed25519_key[1].verify(signature, message)
    with pytest.raises(InvalidSignature):
        ed25519_key[1].verify(signature, message + b"x")


@pytest.mark.parametrize(
    ("interval", "ktype"),
    [
        ("PT1M", "1"),
        ("PT5M", "6"),
        ("PT15M", "7"),
        ("PT30M", "8"),
        ("PT1H", "9"),
        ("P1D", "2"),
        ("P1W", "3"),
        ("P1M", "4"),
    ],
)
def test_interval_ktypes(
    interval: str,
    ktype: str,
    futunn_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(futunn_settings)
    asyncio.run(adapter.fetch_bars(_query(interval=Interval.parse(interval))))

    query_string = urllib.parse.urlsplit(_last_call()["url"]).query
    assert urllib.parse.parse_qs(query_string)["ktype"] == [ktype]


@pytest.mark.parametrize(
    ("adjust", "autype"),
    [(Adjust.NONE, "0"), (Adjust.QFQ, "1"), (Adjust.HFQ, "2")],
)
def test_adjust_autypes(
    adjust: Adjust,
    autype: str,
    futunn_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(futunn_settings)
    asyncio.run(adapter.fetch_bars(_query(adjust=adjust)))

    query_string = urllib.parse.urlsplit(_last_call()["url"]).query
    assert urllib.parse.parse_qs(query_string)["autype"] == [autype]


def test_minute_end_boundary_next_day(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分钟 K 线上游 end 以该日 00:00 截止、不含当日：end 日期 +1 天补偿
    （闭区间复核在适配器完成，多出的尾部数据被过滤）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(futunn_settings)
    query = _query(
        interval=Interval.parse("PT1M"),
        start=datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        end=datetime(2026, 1, 10, 15, 30, tzinfo=CN_TIMEZONE),
    )
    asyncio.run(adapter.fetch_bars(query))

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(_last_call()["url"]).query)
    assert qs["start"] == ["2026-01-01"]
    assert qs["end"] == ["2026-01-11"]  # end 日期 +1 天（00:00 截止语义）


def test_daily_end_boundary_same_day(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日/周/月 K 线 end 含当日：不做 +1 天补偿。"""
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(futunn_settings)
    query = _query(
        start=datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        end=datetime(2026, 1, 10, 15, 30, tzinfo=CN_TIMEZONE),
    )
    asyncio.run(adapter.fetch_bars(query))

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(_last_call()["url"]).query)
    assert qs["end"] == ["2026-01-10"]


def test_us_market_boundary_date_conversion(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """边界日期按标的市场时区换算：北京时间 2026-01-01 00:00 = 纽约
    2025-12-31 11:00（纽约日历日期），分钟 end 再 +1 天。"""
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(futunn_settings)
    query = _query(
        instrument=Instrument.parse("US-NASDAQ-AAPL"),
        interval=Interval.parse("PT5M"),
        start=datetime(2026, 1, 1, tzinfo=CN_TIMEZONE),
        end=datetime(2026, 1, 10, 9, 0, tzinfo=CN_TIMEZONE),  # 纽约 01-09 20:00
    )
    asyncio.run(adapter.fetch_bars(query))

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(_last_call()["url"]).query)
    assert qs["start"] == ["2025-12-31"]  # 纽约日历日期（瞬间所在日）
    assert qs["end"] == ["2026-01-10"]  # 纽约 01-09 + 分钟 +1 天


@pytest.mark.parametrize(
    ("code", "futu"),
    [
        ("CN-SSE-600000", "SH.600000"),
        ("CN-SZSE-000001", "SZ.000001"),
        ("CN-BSE-430047", "BJ.430047"),
        ("HK-HKEX-00700", "HK.00700"),
        ("HK-HKEX-700", "HK.00700"),  # 港股短码零填充 5 位
        ("US-NASDAQ-AAPL", "US.AAPL"),
        ("US-NYSE-TTE", "US.TTE"),
        ("US-AMEX-PLUG", "US.PLUG"),
    ],
)
def test_symbol_mapping(
    code: str,
    futu: str,
    futunn_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(futunn_settings)
    asyncio.run(adapter.fetch_bars(_query(instrument=Instrument.parse(code))))

    assert _last_call()["url"].startswith(f"{REST_BASE}/api/v1.0/quote/{futu}/")


def test_otc_exchange_rejected(futunn_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """美股 OTC 上游不支持获取（品类表外）：exchange 白名单明确拒绝。"""
    install(monkeypatch)
    adapter = _adapter(futunn_settings)
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(
            adapter.fetch_bars(_query(instrument=Instrument.parse("US-OTC-PINK")))
        )


def test_etf_bse_rejected(futunn_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """上游品类表 BJ 无 ETF（仅正股/指数）：ETF×BSE 组合明确拒绝。"""
    install(monkeypatch)
    adapter = _adapter(futunn_settings)
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(
            adapter.fetch_bars(
                _query(
                    asset_type=AssetType.ETF,
                    instrument=Instrument.parse("CN-BSE-430047"),
                )
            )
        )


def test_non_digit_symbol_rejected(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CN/HK 前缀要求纯数字代码：字母代码明确拒绝而非透传上游。"""
    install(monkeypatch)
    adapter = _adapter(futunn_settings)
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(
            adapter.fetch_bars(_query(instrument=Instrument.parse("CN-SSE-60000A")))
        )


def test_us_symbol_with_slash_rejected(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """美股 symbol 含 `/` 明确拒绝（审查项 L8）：`/` 会拼入请求 path 形成
    多余路径段（上游行为未定义）；BRK/A 类 class-share 记法的上游原生
    写法未探针核实（开放问题见 docs/providers/futunn.md），核实前不接受
    任何含 `/` 的替代表示。"""
    install(monkeypatch)
    adapter = _adapter(futunn_settings)
    with pytest.raises(UnsupportedCapabilityError, match="class-share"):
        asyncio.run(
            adapter.fetch_bars(_query(instrument=Instrument.parse("US-NYSE-BRK/A")))
        )


def test_us_symbol_with_dot_passes_through(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不含 `/` 的替代记法（BRK.A）不受影响，原样透传待上游验证。"""
    install(monkeypatch)
    preset_payload(monkeypatch, kline_page([DAY1_MS]))
    adapter = _adapter(futunn_settings)
    asyncio.run(adapter.fetch_bars(_query(instrument=Instrument.parse("US-NYSE-BRK.A"))))
    assert _last_call()["url"].startswith(f"{REST_BASE}/api/v1.0/quote/US.BRK.A/")


def test_same_token_loop_bounded_by_max_pages(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游签发永不推进的 page_token（同令牌循环，backlog #3）：分页循环
    由 ``_MAX_PAGES`` 兜底终止而非死循环；每页行如实拼接（上游异常行为
    由上游负责，护栏只保证终止）；行数未触 UPSTREAM_MAX_BARS → partial
    不误报。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        kline_page([DAY1_MS], pagination={"has_more": True, "next_page_token": "tok-x"}),
    )
    adapter = _adapter(futunn_settings)
    batch = asyncio.run(adapter.fetch_bars(_query()))

    from tapehub.providers.futunn.adapter import _MAX_PAGES

    client = StubClient.instances[-1]
    assert len(client.calls) == _MAX_PAGES  # 兜底终止：恰好 15 页
    assert batch.count == _MAX_PAGES  # 每页同 1 行如实拼接
    assert batch.partial is False  # 未触 5550 上限，不误报截断


def test_unsupported_asset_type_rejected_by_operation_routing(
    futunn_settings: Any,
) -> None:
    """manifest 仅声明 stock/index/etf：期货/外汇请求在二级路由拒绝。"""
    adapter = _adapter(futunn_settings)
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_operation(
            _query(
                asset_type=AssetType.CONTRACT,
                instrument=Instrument.parse("CN-SHFE-CU2601"),
            )
        )
    with pytest.raises(UnsupportedCapabilityError):
        adapter.resolve_quote_operation(
            _quote_query(
                asset_type=AssetType.FOREX,
                instrument=Instrument.parse("GLOBAL-OTC-EUR/USD"),
            )
        )


# ---------------------------------------------------------------------------
# 分页循环与闭区间复核
# ---------------------------------------------------------------------------


def test_page_token_continuation(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """page_token 分页循环：末页无令牌停止；续页 URL 携带 page_token。"""
    install(monkeypatch)
    preset_pages(
        monkeypatch,
        [
            kline_page(
                [DAY1_MS, DAY2_MS],
                pagination={"has_more": True, "next_page_token": "tok-1"},
            ),
            kline_page([DAY10_MS]),
        ],
    )
    adapter = _adapter(futunn_settings)
    batch = asyncio.run(adapter.fetch_bars(_query()))

    calls = [call for client in StubClient.instances for call in client.calls]
    assert len(calls) == 2
    assert "page_token" not in calls[0]["url"]
    assert "page_token=tok-1" in calls[1]["url"]
    assert batch.count == 3


def test_empty_kline_list_yields_empty_batch(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游合法无数据返回 kline_list=[]：映射为空批次（partial False）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"kline_list": []}))
    adapter = _adapter(futunn_settings)
    batch = asyncio.run(adapter.fetch_bars(_query()))

    assert batch.count == 0
    assert batch.partial is False


def test_closed_interval_filter_and_pagination(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    # 窗口 [2026-01-01, 2026-01-10] 北京时间闭区间：两端点窗内、首尾窗外
    preset_payload(
        monkeypatch,
        kline_page(
            [
                DAY1_MS - _DAY_MS,  # 2025-12-31，窗前
                DAY1_MS,  # start 端点（闭区间含）
                DAY10_MS,  # end 端点（闭区间含）
                DAY10_MS + _DAY_MS,  # 2026-01-11，窗外
            ]
        ),
    )
    adapter = _adapter(futunn_settings)
    batch = asyncio.run(adapter.fetch_bars(_query(offset=1, limit=1)))

    assert batch.count == 1
    assert batch.offset == 1
    assert batch.bars[0].timestamp == datetime(2026, 1, 10, 0, tzinfo=CN_TIMEZONE)


def test_volume_precision_restored(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """data.volume_precision=2：行内 volume 已放大 10^2 倍，逐行除以还原。"""
    install(monkeypatch)
    page = kline_page([DAY1_MS], volume_precision=2)
    page["data"]["kline_list"][0]["volume"] = 100000
    preset_payload(monkeypatch, page)
    adapter = _adapter(futunn_settings)
    batch = asyncio.run(adapter.fetch_bars(_query()))

    assert batch.bars[0].volume == 1000.0


def test_partial_when_page_cap_reached(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """触页数上限（15×370=5550）且最早一根晚于 start：partial=True
    （从最新往回取型的隐式截断，与 twelvedata 同语义）。"""
    install(monkeypatch)
    start_key = DAY1_MS + 4 * _DAY_MS  # 2026-01-05（晚于 start 2026-01-01）
    pages = []
    for page_index in range(15):
        keys = [start_key + (page_index * 370 + i) * _DAY_MS for i in range(370)]
        pages.append(
            kline_page(
                keys,
                pagination={"has_more": True, "next_page_token": f"tok-{page_index}"},
            )
        )
    preset_pages(monkeypatch, pages)
    adapter = _adapter(futunn_settings)
    batch = asyncio.run(adapter.fetch_bars(_query(limit=100)))

    assert batch.partial is True
    # 闭区间复核后窗口内仅 6 根（2026-01-05..01-10），partial 语义针对上游截断
    assert batch.count == 6


def test_bar_missing_time_key_contract_violation(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bar 行缺 time_key 属上游契约违背：明确抛 UPSTREAM_ERROR（时间戳是
    bar 的身份键，loud 失败优于静默丢行）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"kline_list": [{"open": 1.0, "close": 1.0}]}))
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["field"] == "time_key"


# ---------------------------------------------------------------------------
# 信封错误映射
# ---------------------------------------------------------------------------


def test_envelope_rate_limited(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ret_code=-11（rate_limited，官方错误码表）→ UPSTREAM_RATE_LIMITED。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch, envelope(None, ret_code=-11, ret_msg="rate limit exceeded")
    )
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamRateLimitedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["upstream_code"] == -11
    assert excinfo.value.details["upstream_message"] == "rate limit exceeded"


def test_envelope_rate_limited_new_style_wrapper(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新封装（code/message，无 ret_code）：同样映射限流（两种封装并存）。"""
    install(monkeypatch)
    preset_payload(monkeypatch, {"code": -11, "message": "rate limit exceeded"})
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamRateLimitedError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_envelope_business_error(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """其余非 0 业务码（如 -7 invalid_symbol）：UPSTREAM_ERROR 携上游码。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(None, ret_code=-7, ret_msg="invalid_symbol"))
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["upstream_code"] == -7
    assert excinfo.value.details["upstream_message"] == "invalid_symbol"


def test_envelope_no_ret_code_rejected(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """信封无 ret_code/code 键：契约违背拒绝。"""
    install(monkeypatch)
    preset_payload(monkeypatch, {"unexpected": True})
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_http_error_status(futunn_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 401（如 -12006 鉴权失败）：契约外防御路径（携 status_code）。"""
    install(monkeypatch)
    preset_status(monkeypatch, 401, {"error": {"code": -12006}})
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["status_code"] == 401


def test_non_json_response(futunn_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    preset_status(monkeypatch, 200, RuntimeError("bad body"))  # 触发 .json() 解析失败
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_data_not_object_rejected(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ret_code=0 但 data 非对象（如 null）：信封契约违背拒绝。"""
    install(monkeypatch)
    preset_payload(monkeypatch, envelope(None))
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_kline_list_not_array_rejected(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"kline_list": "oops"}))
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_bars(_query()))


# ---------------------------------------------------------------------------
# 运行前提
# ---------------------------------------------------------------------------


def test_missing_app_key(tmp_path: Any) -> None:
    adapter = _adapter(
        make_settings(futunn_app_key="", futunn_private_key_path=str(tmp_path / "k.pem"))
    )
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["setting"] == "TAPEHUB_FUTUNN_APP_KEY"


def test_missing_private_key_path() -> None:
    adapter = _adapter(
        make_settings(futunn_app_key="app-key", futunn_private_key_path="")
    )
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert excinfo.value.details["setting"] == "TAPEHUB_FUTUNN_PRIVATE_KEY_PATH"


def test_missing_private_key_file(tmp_path: Any) -> None:
    adapter = _adapter(
        make_settings(
            futunn_app_key="app-key",
            futunn_private_key_path=str(tmp_path / "missing.pem"),
        )
    )
    with pytest.raises(ProviderNotImplementedError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_invalid_private_key_pem(tmp_path: Any) -> None:
    key_path = tmp_path / "bad.pem"
    key_path.write_bytes(b"not a pem")
    adapter = _adapter(
        make_settings(futunn_app_key="app-key", futunn_private_key_path=str(key_path))
    )
    with pytest.raises(ProviderNotImplementedError):
        asyncio.run(adapter.fetch_bars(_query()))


def test_missing_httpx2(rsa_key: tuple[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "httpx2", None)  # 模拟依赖未安装
    adapter = _adapter(
        make_settings(futunn_app_key="app-key", futunn_private_key_path=rsa_key[0])
    )
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "httpx2" in str(excinfo.value)


def test_missing_cryptography(
    rsa_key: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 模拟依赖未安装：签名延迟导入的是 cryptography 子模块路径，子模块可能
    # 已被本测试文件顶层导入缓存——全部置 None 哨兵才会触发 ImportError；
    # 同时装 httpx2 桩兜底保证零网络
    install(monkeypatch)
    for name in [
        module
        for module in sys.modules
        if module == "cryptography" or module.startswith("cryptography.")
    ]:
        monkeypatch.setitem(sys.modules, name, None)
    adapter = _adapter(
        make_settings(futunn_app_key="app-key", futunn_private_key_path=rsa_key[0])
    )
    with pytest.raises(ProviderNotImplementedError) as excinfo:
        asyncio.run(adapter.fetch_bars(_query()))
    assert "cryptography" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 实时快照（REST 单次调用语义）
# ---------------------------------------------------------------------------


def test_stock_quote_request_and_mapping(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, quote_payload([quote_row("SH.600000", QUOTE_TIME_MS)]))
    adapter = _adapter(futunn_settings)

    quote = asyncio.run(adapter.fetch_quote(_quote_query()))

    call = _last_call()
    assert call["method"] == "POST"
    assert call["url"] == f"{REST_BASE}/api/v1.0/quote/stock-quote"
    # code_list 单标的语义传 1 个；body 即签名输入（紧凑分隔符官方示例形态）
    assert call["content"] == b'{"code_list":["SH.600000"]}'
    assert call["headers"]["X-Api-Key"] == "test-app-key"

    assert quote.provider == "futunn"
    # data_time 毫秒戳（绝对时刻）→ 统一北京时间
    assert quote.timestamp == datetime(2026, 1, 1, 15, 0, tzinfo=CN_TIMEZONE)
    assert quote.last_price == 100.0
    assert quote.volume == 12345.0
    assert quote.amount == 1234500.0
    assert quote.bid_price1 is None  # 上游轻量快照无盘口五档
    # extensions 白名单透出（中文名 name 不透出）
    assert quote.extensions == {
        "open_price": 99.0,
        "high_price": 101.0,
        "low_price": 98.0,
        "prev_close_price": 99.5,
        "turnover_rate": 1.23,
        "amplitude": 3.03,
        "suspension": False,
    }


def test_hk_quote_symbol_zero_pad(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """港股短码快照：code_list 零填充 5 位（700 → HK.00700）。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch, quote_payload([quote_row("HK.00700", QUOTE_TIME_MS)])
    )
    adapter = _adapter(futunn_settings)
    quote = asyncio.run(
        adapter.fetch_quote(
            _quote_query(instrument=Instrument.parse("HK-HKEX-700"))
        )
    )

    assert _last_call()["content"] == b'{"code_list":["HK.00700"]}'
    assert quote.last_price == 100.0


def test_quote_suspension_true_in_extensions(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(
        monkeypatch,
        quote_payload([quote_row("SH.600000", QUOTE_TIME_MS, suspension=True)]),
    )
    adapter = _adapter(futunn_settings)
    quote = asyncio.run(adapter.fetch_quote(_quote_query()))

    assert quote.extensions is not None
    assert quote.extensions["suspension"] is True


def test_quote_extensions_only_present_fields(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """白名单字段缺失不透出（不冒充）。"""
    install(monkeypatch)
    row = quote_row("SH.600000", QUOTE_TIME_MS)
    del row["turnover_rate"]
    del row["amplitude"]
    preset_payload(monkeypatch, quote_payload([row]))
    adapter = _adapter(futunn_settings)
    quote = asyncio.run(adapter.fetch_quote(_quote_query()))

    assert quote.extensions is not None
    assert "turnover_rate" not in quote.extensions
    assert "amplitude" not in quote.extensions


def test_quote_no_matching_code_raises(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游对非法 code 静默丢弃（ret_code 仍 0）：匹配行缺失属无有效行情。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch, quote_payload([quote_row("SZ.000001", QUOTE_TIME_MS)])
    )
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(_quote_query()))


def test_quote_invalid_data_time_raises(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """data_time=0（官方「不适用品类」语义）：无有效行情时间，契约违背拒绝。"""
    install(monkeypatch)
    preset_payload(
        monkeypatch, quote_payload([quote_row("SH.600000", 0)])
    )
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert excinfo.value.details["field"] == "data_time"


def test_quote_no_last_price_raises(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(
        monkeypatch, quote_payload([quote_row("SH.600000", QUOTE_TIME_MS, last_price=None)])
    )
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(adapter.fetch_quote(_quote_query()))
    assert excinfo.value.details["field"] == "last_price"


def test_quote_missing_quote_list_raises(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch)
    preset_payload(monkeypatch, envelope({"foo": 1}))
    adapter = _adapter(futunn_settings)
    with pytest.raises(UpstreamError):
        asyncio.run(adapter.fetch_quote(_quote_query()))


def test_quote_missing_credentials() -> None:
    adapter = _adapter(
        make_settings(futunn_app_key="", futunn_private_key_path="")
    )
    with pytest.raises(ProviderNotImplementedError):
        asyncio.run(adapter.fetch_quote(_quote_query()))


def test_upstream_message_truncated_in_details(
    futunn_settings: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """超长上游报文：details 携带截断文本，全文只进服务端日志（信息泄露纵深）。"""
    import logging

    install(monkeypatch)
    long_message = "机密" * 500  # 1000 字符
    preset_payload(monkeypatch, envelope(None, ret_code=-7, ret_msg=long_message))
    adapter = _adapter(futunn_settings)
    with caplog.at_level(logging.WARNING, logger="tapehub.providers.futunn.adapter"):
        with pytest.raises(UpstreamError) as excinfo:
            asyncio.run(adapter.fetch_bars(_query()))
    truncated = excinfo.value.details["upstream_message"]
    assert truncated == long_message[:200] + "…[truncated]"  # 保留前 200 字符
    warnings = [r for r in caplog.records if r.name == "tapehub.providers.futunn.adapter"]
    assert any(long_message in r.getMessage() for r in warnings)  # 全文留在日志
