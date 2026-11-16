"""RequestContextMiddleware 访问日志测试。

覆盖两条路径：

- 正常响应：日志记录方法/路径/状态码/耗时/request_id，响应头回写 request_id；
- call_next 异常穿透（正常由内层异常处理器兜住转 500，穿到中间件属边缘
  路径）：仍补记访问行（500）再重抛，避免 5xx 请求在访问日志中缺失。

Starlette 中间件栈为 ServerErrorMiddleware → 用户中间件 → ExceptionMiddleware →
router：未注册 handler 的 RuntimeError 穿过 ExceptionMiddleware、在用户中间件
call_next 处抛出（触发本测试目标），最终由 ServerErrorMiddleware 接住。
"""

import logging

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tapehub.api.middleware import RequestContextMiddleware


def _make_app() -> Starlette:
    app = Starlette()
    app.add_middleware(RequestContextMiddleware)

    async def ok(request: Request) -> JSONResponse:  # pragma: no cover - 简单桩
        return JSONResponse({"ok": True})

    async def boom(request: Request) -> JSONResponse:
        raise RuntimeError("boom")

    app.add_route("/ok", ok)
    app.add_route("/boom", boom)
    return app


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """只取 tapehub.access 的日志记录（TestClient 自身的 httpx2 日志混在捕获里）。"""
    return [r for r in caplog.records if r.name == "tapehub.access"]


def test_access_log_on_normal_response(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="tapehub.access"):
        with TestClient(_make_app()) as client:
            response = client.get("/ok")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")
    records = _access_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert "GET /ok -> 200" in records[0].getMessage()
    assert f"request_id={response.headers['X-Request-ID']}" in records[0].getMessage()


def test_access_log_on_unhandled_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """call_next 抛异常：访问行补记为 500 且异常重抛（不吞异常）。"""
    with caplog.at_level(logging.INFO, logger="tapehub.access"):
        with TestClient(_make_app(), raise_server_exceptions=True) as client:
            with pytest.raises(RuntimeError, match="boom"):
                client.get("/boom")
    records = _access_records(caplog)
    assert len(records) == 1
    assert "GET /boom -> 500" in records[0].getMessage()
    assert "request_id=req_" in records[0].getMessage()
