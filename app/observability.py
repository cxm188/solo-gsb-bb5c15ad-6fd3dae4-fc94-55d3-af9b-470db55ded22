"""请求级可观测性：request_id 上下文、统一错误响应与结构化访问日志。

- 每个请求生成唯一 request_id，通过响应头 ``X-Request-ID`` 回传；
- 请求局部上下文（:class:`contextvars.ContextVar`）把同一 ID 传给路由与
  异常处理器，基于 asyncio 任务天然隔离，并发请求互不串号；
- 参数校验错误（Pydantic 422）、业务 HTTP 错误与未捕获异常统一为
  含 request_id / 错误码 / 可读信息的 JSON 响应，500 不回传内部异常文本；
- 每个请求结束时输出一条结构化 JSON 访问日志（异常路径同样记录）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

REQUEST_ID_HEADER = "X-Request-ID"

# 请求局部上下文：同一请求内的路由、工具函数与异常处理器读到的都是该请求 ID；
# 不同请求运行在不同 asyncio 任务（各自持有独立 context 副本），天然不串号。
_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """获取当前请求的 request_id；不在请求上下文中时返回 None。"""
    return _request_id_var.get()


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """把日志记录序列化为单行 JSON（时间用 UTC ISO8601，中文不转义）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            )
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 访问日志等通过 extra 传入的结构化字段
        for key in ("request_id", "method", "path", "status_code", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_LOGGING_CONFIGURED = False
_ACCESS_LOGGER_NAME = "app.access"
_ERROR_LOGGER_NAME = "app.error"


def configure_logging() -> None:
    """配置应用日志：JSON 输出到 stdout，级别读取 LOG_LEVEL（默认 info）。

    幂等；uvicorn 自身的访问日志保持不变，应用日志独立成 "app.*" 记录器。
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    level_name = os.environ.get("LOG_LEVEL", "info").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())

    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    app_logger.handlers = [handler]
    app_logger.propagate = False

    _LOGGING_CONFIGURED = True


access_logger = logging.getLogger(_ACCESS_LOGGER_NAME)
error_logger = logging.getLogger(_ERROR_LOGGER_NAME)


# ---------------------------------------------------------------------------
# 统一错误响应
# ---------------------------------------------------------------------------

# HTTP 状态码 -> （错误码，可读信息）
_HTTP_STATUS_META: dict[int, tuple[str, str]] = {
    400: ("bad_request", "请求参数有误"),
    401: ("unauthorized", "未通过身份认证"),
    403: ("forbidden", "无权访问该资源"),
    404: ("not_found", "请求的资源不存在"),
    405: ("method_not_allowed", "不支持该 HTTP 方法"),
    409: ("conflict", "请求与当前资源状态冲突"),
    413: ("payload_too_large", "请求体过大"),
    415: ("unsupported_media_type", "不支持的媒体类型"),
    422: ("validation_error", "请求参数校验失败"),
    429: ("too_many_requests", "请求过于频繁，请稍后再试"),
}


def _error_body(
    request_id: str | None, code: str, message: str, details: Any = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "error": {"code": code, "message": message},
    }
    if details is not None:
        body["error"]["details"] = details
    return body


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    request_id = _request_id_var.get()
    return JSONResponse(
        status_code=status_code,
        content=_error_body(request_id, code, message, details),
        headers={REQUEST_ID_HEADER: request_id or "-"},
    )


def _field_path(loc: tuple[Any, ...]) -> str:
    """把 pydantic 的 loc（如 ('body', 'intersections', 0, 'cycle')）压成点路径。"""
    parts = [str(part) for part in loc if part != "body"]
    if not parts:
        return ""
    path = parts[0]
    for part in parts[1:]:
        if part.isdigit():
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pydantic 请求体 / 字段校验失败：422，details 逐条给出字段与原因。"""
    details = []
    for item in jsonable_encoder(exc.errors()):
        details.append(
            {
                "field": _field_path(tuple(item.get("loc", ()))),
                "message": item.get("msg", ""),
                "type": item.get("type", ""),
            }
        )
    return _error_response(
        status_code=422,
        code="validation_error",
        message="请求参数校验失败",
        details=details,
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """业务侧主动抛出的 HTTPException（本服务主要是跨字段校验 422）。"""
    code, message = _HTTP_STATUS_META.get(
        exc.status_code,
        (
            "http_error" if exc.status_code < 500 else "upstream_error",
            "请求处理失败",
        ),
    )
    details: Any = None
    if exc.detail is not None and exc.detail != "":
        # schemas 层的跨字段校验把中文原因放在 detail 列表里；其余形式原样透传
        if isinstance(exc.detail, list):
            details = jsonable_encoder(exc.detail)
        elif isinstance(exc.detail, str):
            # 保留框架默认信息（如 "Not Found"）之外的可读内容
            if exc.detail not in ("Not Found", "Method Not Allowed"):
                details = [exc.detail]
        else:
            details = jsonable_encoder(exc.detail)
    return _error_response(
        status_code=exc.status_code, code=code, message=message, details=details
    )


# ---------------------------------------------------------------------------
# 请求 ID 中间件 + 访问日志
# ---------------------------------------------------------------------------


async def request_context_middleware(request: Request, call_next) -> JSONResponse:
    """为每个请求建立独立的 request_id 上下文并记录访问日志。

    未捕获异常在此兜底为 500：响应仍带 X-Request-ID，访问日志照记，
    完整堆栈只进服务端日志、不回传客户端。
    """
    request_id = str(uuid.uuid4())
    token = _request_id_var.set(request_id)
    start = time.perf_counter()
    status_code = 500
    try:
        try:
            response = await call_next(request)
        except Exception:
            # 完整内部异常（含堆栈）只写日志；响应体给通用文案。
            error_logger.exception(
                "unhandled exception during request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            response = _error_response(
                status_code=500,
                code="internal_error",
                message="服务器内部错误，请稍后重试",
            )
        status_code = response.status_code
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 3)
        if status_code >= 500:
            level = logging.ERROR
        elif status_code >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO
        access_logger.log(
            level,
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        )
        _request_id_var.reset(token)


def install_observability(app) -> None:
    """在 FastAPI 应用上安装 request_id 中间件、统一异常处理器与日志配置。"""
    configure_logging()
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.middleware("http")(request_context_middleware)
