"""响应映射 ±inf 过滤测试（审查项 L2，三家代表覆盖）。

上游数值出现 ±inf 时与 NaN 同语义置 null：JSON 路由经 pydantic 序列化
会把 inf 静默置 null（未声明口径），SSE 帧经 ``json.dumps`` 则输出非法
JSON token（``Infinity``，严格解析器拒收）。缺失即契约违背的字段
（twelvedata price）按既有路径抛 ``UPSTREAM_ERROR``，不伪造。
"""

import pytest

from tapehub.domain.errors import UpstreamError
from tapehub.providers.akshare.response_mapper import to_standard_bars as ak_bars
from tapehub.providers.tqsdk.response_mapper import to_standard_quote as tqsdk_quote
from tapehub.providers.twelvedata.response_mapper import (
    to_standard_bars as td_bars,
)
from tapehub.providers.twelvedata.response_mapper import (
    ws_price_to_quote,
)


def test_akshare_inf_extensions_and_volume_become_null() -> None:
    rows = [
        {
            "日期": "2025-01-02",
            "开盘": 10.0,
            "最高": 11.0,
            "最低": 9.0,
            "收盘": 10.5,
            "成交量": float("-inf"),  # 量侧非有限值 → null
            "成交额": 105000.0,
            "涨跌幅": float("inf"),  # extensions 白名单字段 inf → 不透出
        }
    ]
    bars = ak_bars("stock_zh_a_hist", rows)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.volume is None
    assert bar.extensions is None or "pct_change" not in bar.extensions


def test_tqsdk_quote_inf_last_price_becomes_null() -> None:
    from datetime import datetime

    from tapehub.domain.models import CN_TIMEZONE

    snapshot = {
        "datetime": "2025-01-02 09:30:00.000",
        "last_price": float("inf"),
        "bid_price1": float("nan"),  # NaN 既有行为回归：仍为 null
        "ask_price1": float("-inf"),
        "volume": 0.0,  # 量侧真实 0 保留（既有口径）
    }
    quote = tqsdk_quote(
        snapshot, provider="tqsdk", received_at=datetime.now(CN_TIMEZONE)
    )
    assert quote.last_price is None
    assert quote.bid_price1 is None
    assert quote.ask_price1 is None
    assert quote.volume == 0.0


def test_twelvedata_bars_inf_volume_becomes_null() -> None:
    from tapehub.domain.models import AssetType

    rows = [
        {
            "datetime": "2025-01-02 09:30:00",
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.05,
            "volume": float("inf"),
        }
    ]
    bars = td_bars(rows, AssetType.STOCK)
    assert bars[0].volume is None


def test_twelvedata_price_inf_raises_upstream_error() -> None:
    """price 缺失即契约违背：inf 归一为 None 后走既有抛错路径，不伪造。"""
    from datetime import datetime

    from tapehub.domain.models import CN_TIMEZONE

    with pytest.raises(UpstreamError) as excinfo:
        ws_price_to_quote(
            {"timestamp": 1700000000, "price": float("inf")},
            provider="twelvedata",
            received_at=datetime.now(CN_TIMEZONE),
        )
    assert excinfo.value.details.get("field") == "price"
