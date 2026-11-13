"""测试用 ths HTTP 桩：模拟延迟导入的 ``httpx`` 同步客户端（零网络）。

只实现适配器使用的路径：
- ``httpx.Client(timeout=...)`` 上下文管理器；
- ``client.get(url, params=..., headers=...)``：记录调用（url/params/headers）
  并返回 ``StubResponse``（``status_code`` + ``.json()``）。

响应构造工厂按官方文档的统一 ``ApiResponse`` 信封（code/message/request_id/
data）与各端点 ``data.item`` 行形态（历史 K 线 OHLC、快照、分时点）镜像。
"""

import json
from typing import Any


class StubResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        # httpx.Response.text 等价物（防御路径不依赖，保留对称）
        if isinstance(payload, str):
            self.text = payload
        else:
            try:
                self.text = json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError):
                self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise ValueError("invalid json")  # 模拟 JSON 解析失败
        return self._payload


class StubClient:
    """httpx.Client 桩；所有实例记录在 ``instances``。"""

    instances: list["StubClient"] = []

    def __init__(self, timeout: Any = None, **kwargs: Any) -> None:
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []
        self.status_code = 200
        self.payload: Any = []
        StubClient.instances.append(self)

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> StubResponse:
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        return StubResponse(self.status_code, self.payload)

    def __enter__(self) -> "StubClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def install(monkeypatch: Any) -> type[StubClient]:
    """把桩模块注入 sys.modules，清空实例记录。"""
    import sys
    import types

    module = types.ModuleType("httpx")
    module.Client = StubClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", module)
    StubClient.instances.clear()
    return StubClient


def preset_payload(monkeypatch: Any, payload: Any) -> None:
    """让每个新建的桩客户端返回指定 payload（须在 install 之后调用）。"""
    original_init = StubClient.__init__

    def init_with_payload(self: StubClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.payload = payload

    monkeypatch.setattr(StubClient, "__init__", init_with_payload)


def preset_status(monkeypatch: Any, status_code: int, payload: Any) -> None:
    """让每个新建的桩客户端返回指定状态码与 payload（须在 install 之后调用）。"""
    original_init = StubClient.__init__

    def init_with_status(self: StubClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.status_code = status_code
        self.payload = payload

    monkeypatch.setattr(StubClient, "__init__", init_with_status)


def envelope(data: Any, *, code: int = 0, message: str = "success") -> dict[str, Any]:
    """统一 ApiResponse 信封（业务结果含错误 HTTP 恒 200，经 code 表达）。"""
    return {
        "code": code,
        "message": message,
        "request_id": "stub-request-id",
        "data": data,
    }


def bar_rows(date_key: str, dates_ms: list[int]) -> list[dict[str, Any]]:
    """历史 K 线行（A 股/指数/ETF 用 ``date_ms``，期货/期权用 ``timestamp``）。

    字段形态镜像官方文档：open_price/high_price/low_price/close_price/
    volume/turnover。
    """
    return [
        {
            date_key: day_ms,
            "open_price": 10.0 + index,
            "high_price": 11.0 + index,
            "low_price": 9.5 + index,
            "close_price": 10.5 + index,
            "volume": 1000 + index,
            "turnover": 10500.0 + index,
        }
        for index, day_ms in enumerate(dates_ms)
    ]


def snapshot_data(timestamp_ms: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    """快照 ``data`` 容器（A 股/指数/ETF snapshot：timestamp + item[]）。

    ``items`` 为 item 行列表（空列表镜像上游「合法无数据返回 item=[]」）。
    """
    return {"timestamp": timestamp_ms, "total": len(items), "item": list(items)}


def stock_snapshot_item() -> dict[str, Any]:
    """A 股快照行（官方文档示例形态镜像；快照不返回中文名 name）。"""
    return {
        "thscode": "600519.SH",
        "ticker": "600519",
        "volume": 3098875,
        "turnover": 3937375200,
        "last_price": 1277.8,
        "price_change": 21.8,
        "price_change_ratio_pct": 1.735669,
        "open_price": 1252.08,
        "high_price": 1282,
        "low_price": 1250.21,
        "prev_price": 1256,
    }


def intraday_data(timestamp_ms: int, points: list[dict[str, Any]]) -> dict[str, Any]:
    """分时 ``data`` 容器（期货/期权 intraday：timestamp/thscode/date/session +
    item[] 分时点）。"""
    return {
        "timestamp": timestamp_ms,
        "thscode": "CU2601.SHF",
        "date": "2026-01-09",
        "session": "intraday",
        "item": points,
    }


def intraday_points(
    timestamps_ms: list[int],
    *,
    prices: list[float],
    volumes: list[float] | None = None,
    turnovers: list[float] | None = None,
) -> list[dict[str, Any]]:
    """分时点列表（timestamp/price/volume/turnover）。

    ``volumes``/``turnovers`` 缺省时逐点恒值（12 / 4692000.0）；分时点
    量额为逐分钟增量口径（2026-11-13 探针实证），当日累计 = 全点求和。
    """
    return [
        {
            "timestamp": ts,
            "price": price,
            "volume": volumes[index] if volumes is not None else 12,
            "turnover": turnovers[index] if turnovers is not None else 4692000.0,
        }
        for index, (ts, price) in enumerate(zip(timestamps_ms, prices, strict=True))
    ]
