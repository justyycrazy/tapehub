"""测试用 futunn HTTP 桩：模拟延迟导入的 ``httpx2`` 同步客户端（零网络）。

只实现适配器使用的路径：
- ``httpx2.Client(timeout=...)`` 上下文管理器；
- ``client.get(url, headers=...)`` / ``client.post(url, content=..., headers=...)``：
  记录调用（url/headers/content）并返回 ``StubResponse``（``status_code`` +
  ``.json()`` + ``headers``，GET 无 params 参数——查询串已在 URL 内，
  与签名一致性由 request_mapper 单点保证）。

响应构造工厂按官方文档的信封形态（ret_code/ret_msg/data + 顶层
pagination）与各端点数据形态（data.kline_list 行、data.quote_list 行）
镜像。签名不在桩内 Mock：测试用 cryptography 现场生成私钥 PEM 走真实
签名路径（见 test_futunn_provider.py）。
"""

import json
from typing import Any


class StubResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        # httpx2.Response.text 等价物（防御路径不依赖，保留对称）
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
    """httpx2.Client 桩；所有实例记录在 ``instances``。

    适配器为实例级共享客户端（懒创建复用）：preset 经**类级**值在请求时
    读取——``preset_payload`` 多次调用以最后一次为准；``preset_pages`` 按
    **请求次序**（call 序）出队，语义即「第 N 次请求返回第 N 页」，与共享
    client 及逐请求新建两种模型都兼容。
    """

    instances: list["StubClient"] = []
    _preset_status_code: int = 200
    _preset_payload: Any = []
    _page_queue: list[Any] = []  # preset_pages 的请求次序 payload 队列

    def __init__(self, timeout: Any = None, **kwargs: Any) -> None:
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []
        StubClient.instances.append(self)

    def _next_payload(self) -> Any:
        cls = type(self)
        if cls._page_queue:
            return cls._page_queue.pop(0)
        return cls._preset_payload

    def get(
        self, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> StubResponse:
        self.calls.append(
            {"method": "GET", "url": url, "headers": dict(headers or {}), "content": None}
        )
        return StubResponse(type(self)._preset_status_code, self._next_payload())

    def post(
        self,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> StubResponse:
        self.calls.append(
            {"method": "POST", "url": url, "headers": dict(headers or {}), "content": content}
        )
        return StubResponse(type(self)._preset_status_code, self._next_payload())

    def close(self) -> None:
        """停机钩子（shutdown_runtime）调用：桩无连接池，no-op。"""
        return None

    def __enter__(self) -> "StubClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def install(monkeypatch: Any) -> type[StubClient]:
    """把桩模块注入 sys.modules，清空实例记录与类级 preset/页队列。"""
    import sys
    import types

    module = types.ModuleType("httpx2")
    module.Client = StubClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx2", module)
    StubClient.instances.clear()
    StubClient._preset_status_code = 200
    StubClient._preset_payload = []
    StubClient._page_queue = []
    return StubClient


def preset_payload(monkeypatch: Any, payload: Any) -> None:
    """预置后续请求返回的 payload（请求时读类级值，多次调用以最后一次为准）。"""
    monkeypatch.setattr(StubClient, "_preset_payload", payload, raising=False)


def preset_pages(monkeypatch: Any, payloads: list[Any]) -> None:
    """按请求次序依次返回 payload（bars page_token 多页循环测试）；
    请求次数超出队列时沿用最后一个。"""
    monkeypatch.setattr(StubClient, "_preset_payload", payloads[-1], raising=False)
    monkeypatch.setattr(StubClient, "_page_queue", list(payloads), raising=False)


def preset_status(monkeypatch: Any, status_code: int, payload: Any) -> None:
    """预置后续请求返回的状态码与 payload（请求时读类级值）。"""
    monkeypatch.setattr(StubClient, "_preset_status_code", status_code, raising=False)
    monkeypatch.setattr(StubClient, "_preset_payload", payload, raising=False)


def envelope(
    data: Any,
    *,
    ret_code: int = 0,
    ret_msg: str = "success",
    pagination: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """统一信封（业务结果含错误 HTTP 200，经 ret_code 表达；pagination
    为顶层容器，与 data 平级）。"""
    payload: dict[str, Any] = {"ret_code": ret_code, "ret_msg": ret_msg, "data": data}
    if pagination is not None:
        payload["pagination"] = pagination
    payload.update(extra)
    return payload


def kline_rows(
    time_keys_ms: list[int], *, time_zone: int = 480
) -> list[dict[str, Any]]:
    """历史 K 线行（官方文档行形态镜像：time_key 毫秒戳 + time_zone 相对
    UTC 分钟偏移 + OHLC/volume/turnover；行值按行序递增造数便于断言）。"""
    rows: list[dict[str, Any]] = []
    for index, key in enumerate(time_keys_ms):
        rows.append(
            {
                "time_key": key,
                "time_zone": time_zone,
                "open": 10.0 + index,
                "close": 10.5 + index,
                "high": 11.0 + index,
                "low": 9.5 + index,
                "volume": 1000 + index,
                "turnover": 10500.0 + index,
            }
        )
    return rows


def kline_page(
    time_keys_ms: list[int],
    *,
    time_zone: int = 480,
    volume_precision: int | None = None,
    pagination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """历史 K 线单页信封：data.kline_list + data.volume_precision +
    顶层 pagination（next_page_token/has_more）。"""
    data: dict[str, Any] = {"kline_list": kline_rows(time_keys_ms, time_zone=time_zone)}
    if volume_precision is not None:
        data["volume_precision"] = volume_precision
    return envelope(data, pagination=pagination)


def quote_row(code: str, data_time_ms: int, **overrides: Any) -> dict[str, Any]:
    """快照行（官方文档字段形态镜像：code/data_time/OHLC/前收/量额/
    换手率/振幅/停牌）。"""
    row: dict[str, Any] = {
        "code": code,
        "name": "Sample",
        "data_time": data_time_ms,
        "data_date": "2026-01-01",
        "last_price": 100.0,
        "open_price": 99.0,
        "high_price": 101.0,
        "low_price": 98.0,
        "prev_close_price": 99.5,
        "volume": 12345,
        "turnover": 1234500.0,
        "turnover_rate": 1.23,
        "amplitude": 3.03,
        "suspension": False,
    }
    row.update(overrides)
    return row


def quote_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """快照信封 data 容器（data.quote_list；非法 code 被上游静默丢弃）。"""
    return envelope({"quote_list": rows})


# ---------------------------------------------------------------------------
# 签名测试辅助：现场生成测试密钥对（不落真实凭据，签名走真实 cryptography 路径）
# ---------------------------------------------------------------------------


def write_test_rsa_key(directory: Any) -> tuple[str, Any]:
    """生成 RSA 测试私钥 PEM（PKCS#8 无密码），返回 ``(路径, 公钥对象)``。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = directory / "test-rsa.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(path), key.public_key()


def write_test_ed25519_key(directory: Any) -> tuple[str, Any]:
    """生成 Ed25519 测试私钥 PEM（PKCS#8 无密码），返回 ``(路径, 公钥对象)``。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    path = directory / "test-ed25519.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(path), key.public_key()
