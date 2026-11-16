"""统一异常处理：所有错误都转换为统一错误结构，并携带 request_id。

- Domain 业务错误 → 统一错误码 + 合理 HTTP 状态码；
- FastAPI/Pydantic 参数校验错误 → INVALID_REQUEST；
- SDK/未知异常 → 统一错误，不泄露 Token、账号、堆栈或内部地址。
"""

import logging
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from tapehub.api.schemas.errors import ErrorBody, ErrorResponse
from tapehub.domain.errors import ErrorCode, TapehubError

logger = logging.getLogger(__name__)

_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.PROVIDER_NOT_FOUND: 404,
    ErrorCode.PROVIDER_DISABLED: 409,
    ErrorCode.PROVIDER_NOT_IMPLEMENTED: 501,
    ErrorCode.UNSUPPORTED_CAPABILITY: 422,
    ErrorCode.NO_PROVIDER_AVAILABLE: 409,
    ErrorCode.CONCURRENCY_LIMIT: 429,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.UPSTREAM_RATE_LIMITED: 429,
    ErrorCode.UPSTREAM_ERROR: 502,
    # 快照首包等待超时（订阅已建立、上游未推送）：语义为“请稍后重试”
    ErrorCode.QUOTE_NOT_READY: 408,
    # 行情流客户端上限（服务容量闸门）：与 CONCURRENCY_LIMIT 同为可重试限流
    ErrorCode.STREAM_LIMIT_REACHED: 429,
    # 上游并发订阅上限（如 Twelve Data WS credits 计划额度）：可重试限流
    ErrorCode.UPSTREAM_SUBSCRIPTION_LIMIT: 429,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.INTERNAL_ERROR: 500,
}

_HTTP_FALLBACK: dict[int, tuple[ErrorCode, str]] = {
    404: (ErrorCode.NOT_FOUND, "Resource not found."),
    405: (ErrorCode.METHOD_NOT_ALLOWED, "Method not allowed."),
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or f"req_{uuid.uuid4().hex}"


def error_response(
    request: Request,
    code: ErrorCode,
    message: str,
    details: dict[str, Any],
    *,
    status_code: int | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code.value,
            message=message,
            request_id=_request_id(request),
            details=details,
        )
    )
    # ``status_code`` 覆盖：HTTP 异常透传路径保留原始状态码（如 401），
    # 避免业务码查表把非标准状态覆盖为查表值
    return JSONResponse(
        status_code=status_code or _STATUS_BY_CODE[code], content=body.model_dump()
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(TapehubError)
    async def handle_tapehub_error(request: Request, exc: TapehubError) -> JSONResponse:
        return error_response(request, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "loc": [str(item) for item in error.get("loc", ())],
                "message": str(error.get("msg", "")),
            }
            for error in exc.errors()
        ]
        return error_response(
            request,
            ErrorCode.INVALID_REQUEST,
            "Invalid request parameters.",
            {"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        fallback = _HTTP_FALLBACK.get(
            exc.status_code,
            (
                ErrorCode.INVALID_REQUEST
                if exc.status_code < 500
                else ErrorCode.INTERNAL_ERROR,
                "Request failed.",
            ),
        )
        code, message = fallback
        # 保留原始状态码（统一错误结构不变）：业务码映射表只覆盖 Domain
        # 错误，HTTP 层异常（401/404/405 等）不该被查表值改写
        return error_response(request, code, message, {}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s: %s", type(exc).__name__, exc)
        return error_response(
            request, ErrorCode.INTERNAL_ERROR, "Internal server error.", {}
        )
