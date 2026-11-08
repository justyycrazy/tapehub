"""baostock 适配器单元测试：请求映射、响应映射与会话管理（SDK 全部 Mock）。

baostock 特性覆盖点：
- 单一上游函数 query_history_k_data_plus，全参数显式传递（fields 三套、
  adjustflag、start_date/end_date、frequency）；
- 上游行值全为字符串、缺失为空串 ""（→ null）；
- 错误经 error_code/error_msg 字符串返回而非异常（"0"=成功）；
- login()/logout() 全局单连接会话：懒登录 + 进程内复用 + 未登录/网络类
  错误码重登一次并重试一次。
"""

import asyncio
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tapehub.domain.errors import (
    ProviderNotImplementedError,
    UnsupportedCapabilityError,
    UpstreamError,
)
from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType, BarQuery, DataVariant, Instrument
from tapehub.providers.manifest import load_builtin_manifest
from tapehub.providers.registry import build_provider_record
from tests.fixtures.providers.stub_baostock import (
    StubBaostock,
    StubResultData,
    daily_stock_rows,
    daily_suspended_rows,
    index_rows,
    minute_rows,
    weekly_rows,
)

# CN 标的的时间表达统一用北京时间（领域层会将边界归一化到市场规范时区）
CN = ZoneInfo("Asia/Shanghai")

DAILY_STOCK_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST,peTTM,pbMRQ,psTTM,pcfNcfTTM"
)
DAILY_INDEX_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,pctChg"
WEEKLY_MONTHLY_FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg"
MINUTE_FIELDS = "date,time,code,open,high,low,close,volume,amount,adjustflag"


def _query(**overrides: Any) -> BarQuery:
    base: dict[str, Any] = {
        "asset_type": AssetType.STOCK,
        "instrument": Instrument.parse("CN-SSE-600000"),
        "interval": Interval.parse("P1D"),
        "start": datetime(2026, 1, 1, tzinfo=CN),
        "end": datetime(2026, 1, 10, tzinfo=CN),
        "adjust": Adjust.QFQ,
        "variant": DataVariant.BAOSTOCK,
        "offset": 0,
        "limit": 500,
    }
    base.update(overrides)
    return BarQuery(**base)


def _adapter() -> Any:
    manifest = load_builtin_manifest("baostock")
    return build_provider_record("baostock", manifest, _adapter_cls()).adapter


def _adapter_cls() -> type:
    from tapehub.providers.baostock.adapter import ProviderAdapter

    return ProviderAdapter


def _install_stub(monkeypatch: pytest.MonkeyPatch) -> StubBaostock:
    stub = StubBaostock()
    monkeypatch.setitem(sys.modules, "baostock", stub)
    return stub


def _last_query_kwargs(stub: StubBaostock) -> dict[str, Any]:
    for name, kwargs in reversed(stub.calls):
        if name == "query_history_k_data_plus":
            return kwargs
    raise AssertionError("no query call recorded")


def test_stock_daily_request_and_response_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-05", "2026-01-06"])

    batch = asyncio.run(_adapter().fetch_bars(_query()))

    kwargs = _last_query_kwargs(stub)
    # 所有参数显式传递，不依赖上游默认值（adjustflag 默认 "3"、start 默认 2015-01-01）
    assert kwargs == {
        "code": "sh.600000",
        "fields": DAILY_STOCK_FIELDS,
        "start_date": "2026-01-01",
        "end_date": "2026-01-10",
        "frequency": "d",
        "adjustflag": "2",
    }
    assert batch.provider == "baostock"
    assert batch.count == 2
    bar = batch.bars[0]
    assert bar.timestamp == datetime(2026, 1, 5, tzinfo=CN)
    assert bar.open == 12.64
    assert bar.close == 12.56
    # 上游成交量单位已是股，不做换算
    assert bar.volume == 38778949.0
    assert bar.amount == 486264672.0
    extensions = bar.extensions
    assert extensions is not None
    # extensions 白名单：日线个股全量字段
    assert extensions["prev_close"] == 12.65
    assert extensions["turnover_rate"] == 0.137985
    assert extensions["pct_change"] == -0.711456
    assert extensions["trade_status"] == 1.0
    assert extensions["is_st"] == 0.0
    assert extensions["pe_ttm"] == 5.123456
    # 空串缺失 → null（白名单值不写入）
    assert "pcf_ncf_ttm" not in extensions


@pytest.mark.parametrize(
    ("interval", "frequency", "fields"),
    [
        ("P1D", "d", DAILY_STOCK_FIELDS),
        ("P1W", "w", WEEKLY_MONTHLY_FIELDS),
        ("P1M", "m", WEEKLY_MONTHLY_FIELDS),
        ("PT5M", "5", MINUTE_FIELDS),
        ("PT15M", "15", MINUTE_FIELDS),
        ("PT30M", "30", MINUTE_FIELDS),
        ("PT1H", "60", MINUTE_FIELDS),
    ],
)
def test_interval_frequency_and_fields_mapping(
    monkeypatch: pytest.MonkeyPatch, interval: str, frequency: str, fields: str
) -> None:
    stub = _install_stub(monkeypatch)
    if interval.startswith("PT"):
        stub.result = minute_rows([("2026-01-05", "20260105093500000")])
    else:
        stub.result = daily_stock_rows(["2026-01-05"])

    asyncio.run(_adapter().fetch_bars(_query(interval=Interval.parse(interval))))

    kwargs = _last_query_kwargs(stub)
    assert kwargs["frequency"] == frequency
    assert kwargs["fields"] == fields


@pytest.mark.parametrize(
    ("adjust", "adjustflag"),
    [(Adjust.NONE, "3"), (Adjust.QFQ, "2"), (Adjust.HFQ, "1")],
)
def test_adjust_flag_mapping(
    monkeypatch: pytest.MonkeyPatch, adjust: Adjust, adjustflag: str
) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-05"])

    asyncio.run(_adapter().fetch_bars(_query(adjust=adjust)))

    assert _last_query_kwargs(stub)["adjustflag"] == adjustflag


def test_index_daily_fields_and_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = index_rows(["2026-01-05"])

    query = _query(
        asset_type=AssetType.INDEX,
        instrument=Instrument.parse("CN-SSE-000001"),
        adjust=Adjust.NONE,
    )
    batch = asyncio.run(_adapter().fetch_bars(query))

    kwargs = _last_query_kwargs(stub)
    # 指数日线：无估值字段
    assert kwargs["code"] == "sh.000001"
    assert kwargs["fields"] == DAILY_INDEX_FIELDS
    assert kwargs["adjustflag"] == "3"
    bar = batch.bars[0]
    assert bar.close == 3135.92
    assert bar.volume == 14156718592.0
    extensions = bar.extensions
    assert extensions is not None
    assert extensions["prev_close"] == 3103.637
    assert extensions["pct_change"] == 1.0402
    assert "trade_status" not in extensions
    assert "pe_ttm" not in extensions


def test_weekly_mapping_and_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = weekly_rows(["2026-01-09"])

    batch = asyncio.run(_adapter().fetch_bars(_query(interval=Interval.parse("P1W"))))

    kwargs = _last_query_kwargs(stub)
    assert kwargs["frequency"] == "w"
    assert kwargs["fields"] == WEEKLY_MONTHLY_FIELDS
    bar = batch.bars[0]
    # 周/月线时间戳同为交易日 00:00 北京时间
    assert bar.timestamp == datetime(2026, 1, 9, tzinfo=CN)
    extensions = bar.extensions
    assert extensions is not None
    assert extensions["turnover_rate"] == 1.37985
    assert "prev_close" not in extensions


def test_minute_timestamp_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = minute_rows(
        [("2026-01-05", "20260105093500000"), ("2026-01-05", "20260105101500123")]
    )

    batch = asyncio.run(_adapter().fetch_bars(_query(interval=Interval.parse("PT5M"))))

    kwargs = _last_query_kwargs(stub)
    assert kwargs["frequency"] == "5"
    assert kwargs["fields"] == MINUTE_FIELDS
    # date + time（YYYYMMDDHHMMSSsss）→ 北京时间，毫秒保留
    assert batch.bars[0].timestamp == datetime(2026, 1, 5, 9, 35, 0, tzinfo=CN)
    assert batch.bars[1].timestamp == datetime(2026, 1, 5, 10, 15, 0, 123000, tzinfo=CN)
    # 分钟线无 extensions（fields 无白名单字段）
    assert batch.bars[0].extensions is None


def test_szse_code_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-05"])

    query = _query(instrument=Instrument.parse("CN-SZSE-000001"))
    asyncio.run(_adapter().fetch_bars(query))

    assert _last_query_kwargs(stub)["code"] == "sz.000001"


def test_unsupported_exchange_rejected() -> None:
    # baostock 仅覆盖沪深两市，北交所不声明
    with pytest.raises(UnsupportedCapabilityError):
        asyncio.run(_adapter().fetch_bars(_query(instrument=Instrument.parse("CN-BSE-832000"))))


def test_index_minute_not_declared() -> None:
    # 指数无分钟线（官方明示），manifest 不声明 → 二级路由拒绝
    with pytest.raises(UnsupportedCapabilityError):
        _adapter().resolve_operation(
            _query(asset_type=AssetType.INDEX, interval=Interval.parse("PT5M"))
        )


def test_session_reuse_login_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-05"])
    adapter = _adapter()

    asyncio.run(adapter.fetch_bars(_query()))
    asyncio.run(adapter.fetch_bars(_query()))

    # 懒登录 + 进程内复用：两次请求只登录一次、各查询一次
    assert stub.login_count == 1
    query_calls = [name for name, _ in stub.calls if name == "query_history_k_data_plus"]
    assert len(query_calls) == 2


def test_login_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.login_error_code = "10001011"
    stub.login_error_msg = "黑名单用户，请与管理员联系"

    with pytest.raises(UpstreamError) as exc_info:
        asyncio.run(_adapter().fetch_bars(_query()))

    details = exc_info.value.details
    assert details["upstream_error_code"] == "10001011"
    assert details["upstream_error_msg"] == "黑名单用户，请与管理员联系"


def test_no_login_error_triggers_relogin_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-05"])
    stub.pending_errors = [StubResultData([], [], error_code="10001001", error_msg="用户未登陆")]

    batch = asyncio.run(_adapter().fetch_bars(_query()))

    # 会话失效：重登一次并重试一次，最终成功
    assert stub.login_count == 2
    assert batch.count == 1


@pytest.mark.parametrize("error_code", ["10002002", "10002004"])
def test_network_error_triggers_relogin_retry(
    monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-05"])
    stub.pending_errors = [StubResultData([], [], error_code=error_code, error_msg="网络错误")]

    batch = asyncio.run(_adapter().fetch_bars(_query()))

    assert stub.login_count == 2
    assert batch.count == 1


def test_non_recoverable_error_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = StubResultData([], [], error_code="10004011", error_msg="无效的证券代码")

    with pytest.raises(UpstreamError) as exc_info:
        asyncio.run(_adapter().fetch_bars(_query()))

    # 不可恢复错误不重登（login_count 保持 1），错误码字符串透出 details
    assert stub.login_count == 1
    assert exc_info.value.details["upstream_error_code"] == "10004011"
    assert exc_info.value.details["upstream_error_msg"] == "无效的证券代码"


def test_missing_sdk_returns_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    # sys.modules 中置 None 使 import 失败，模拟 SDK 未安装
    monkeypatch.setitem(sys.modules, "baostock", None)
    with pytest.raises(ProviderNotImplementedError):
        asyncio.run(_adapter().fetch_bars(_query()))


def test_closed_interval_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    # 上游可能返回窗口外的日期行，适配器必须按 [start, end] 精确过滤
    stub.result = daily_stock_rows(["2025-12-31", "2026-01-01", "2026-01-05", "2026-02-01"])

    batch = asyncio.run(_adapter().fetch_bars(_query()))

    timestamps = [bar.timestamp for bar in batch.bars]
    assert timestamps == [
        datetime(2026, 1, 1, tzinfo=CN),
        datetime(2026, 1, 5, tzinfo=CN),
    ]


def test_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-0%d" % d for d in range(1, 10)])

    batch = asyncio.run(_adapter().fetch_bars(_query(offset=2, limit=3)))

    assert batch.offset == 2
    assert batch.limit == 3
    assert batch.count == 3
    assert [bar.timestamp.day for bar in batch.bars] == [3, 4, 5]


def test_suspended_row_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    # 停牌日（探针实证 147 行口径）：OHLC=前收、量额=0、换手率空串 → null
    stub = _install_stub(monkeypatch)
    stub.result = daily_suspended_rows(["2026-01-05"])

    batch = asyncio.run(_adapter().fetch_bars(_query()))

    bar = batch.bars[0]
    assert bar.volume == 0.0  # 上游原值 0，如实透出
    assert bar.amount == 0.0
    extensions = bar.extensions
    assert extensions is not None
    assert "turnover_rate" not in extensions  # 空串缺失 → 不写入


def test_partial_when_row_limit_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    # 上游单次返回上限 5000 根（探针实证）：达到即置 partial（窗口尾部截断）
    stub = _install_stub(monkeypatch)
    stub.result = daily_stock_rows(["2026-01-%02d" % d for d in range(1, 11)] * 500)

    batch = asyncio.run(_adapter().fetch_bars(_query()))
    assert batch.partial is True

    stub.result = daily_stock_rows(["2026-01-05"])
    batch = asyncio.run(_adapter().fetch_bars(_query()))
    assert batch.partial is False
