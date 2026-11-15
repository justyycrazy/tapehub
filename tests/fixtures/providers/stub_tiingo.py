"""测试用 Tiingo HTTP 桩：模拟延迟导入的 ``httpx2`` 同步客户端（零网络）。

只实现适配器使用的路径：
- ``httpx2.Client(timeout=...)`` 上下文管理器；
- ``client.get(url, params=..., headers=...)``：记录调用（url/params/headers）
  并返回 ``StubResponse``（``status_code`` + ``.json()``）。

行造数工厂按官方文档字段结构（crypto 含 volume，forex 仅 OHLC）。
"""

import json
from typing import Any


class StubResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        # httpx2.Response.text 等价物（适配器 400 指纹检查依赖）
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
    """httpx2.Client 桩；所有实例记录在 ``instances``。"""

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
        params: dict[str, str] | None = None,
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

    module = types.ModuleType("httpx2")
    module.Client = StubClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx2", module)
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


def forex_rows(dates: list[str]) -> list[dict[str, Any]]:
    """外汇 /tiingo/fx/<ticker>/prices 行：官方文档仅列 OHLC（无 volume）。"""
    return [
        {
            "date": day,
            "ticker": "eurusd",
            "open": 1.085,
            "high": 1.086,
            "low": 1.084,
            "close": 1.0855,
        }
        for day in dates
    ]


def crypto_rows(dates: list[str]) -> list[dict[str, Any]]:
    """加密 /tiingo/crypto/prices 响应形（探针实证镜像——官方文档未如实
    反映的嵌套结构）：
    按 ticker 分组的外层元素 + ``priceData`` bar 行数组（date 带 +00:00、
    含 volume（base 币计）/volumeNotional/tradesDone）。
    """
    return [
        {
            "ticker": "btcusd",
            "baseCurrency": "btc",
            "quoteCurrency": "usd",
            "priceData": [
                {
                    "date": day,
                    "open": 42000.5,
                    "high": 43500.0,
                    "low": 41800.2,
                    "close": 43120.75,
                    "volume": 12345.6,
                    "volumeNotional": 123.45,
                    "tradesDone": 100,
                }
                for day in dates
            ],
        }
    ]
