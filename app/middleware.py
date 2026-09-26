"""请求级中间件与结构化访问日志。

``RequestContextMiddleware`` 是一个纯 ASGI 中间件（位于 Starlette 异常
中间件外层），负责：

1. 为每个 HTTP 请求生成唯一 request_id（UUID4），绑定到 ContextVar
   请求局部上下文，路由、业务校验与错误处理器共享同一 ID；
2. 在**所有**响应（含 4xx / 500）上回传 ``X-Request-ID`` 响应头；
3. 输出一条结构化 JSON 访问日志，含 request_id、method、path、
   status_code、duration_ms（以及异常路径的 error 信息）；
4. 兜底未捕获异常：记录含完整堆栈的 error 日志，返回不泄漏内部异常
   文本的统一 500 JSON（错误处理器在更外层执行、拿不到请求上下文，
   故 500 兜底放在此处）。

纯 ASGI 实现（而非 BaseHTTPMiddleware）确保异常处理器与中间件运行在
同一任务上下文中，ContextVar 一定可见；每个连接由服务端在独立任务中
调用，ContextVar 天然按请求隔离，并发调用互不串号。
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import REQUEST_ID_HEADER, internal_error_response
from .request_context import reset_request_id, set_request_id

ACCESS_LOGGER_NAME = "greenwave.access"


class JsonLineFormatter(logging.Formatter):
    """把日志记录格式化为单行 JSON（额外字段平铺到顶层）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 结构化访问日志字段由中间件通过 extra 注入。
        for key, value in getattr(record, "log_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["traceback"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        return json.dumps(payload, ensure_ascii=False)


def configure_access_logging() -> logging.Logger:
    """配置结构化访问日志 logger；幂等，重复调用不会叠加 handler。"""
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLineFormatter())
        logger.addHandler(handler)
        logger.propagate = False  # 不向 root logger 冒泡，避免与 uvicorn 日志重复
    # 同时兼容 Dockerfile 的 UVICORN_LOG_LEVEL 与语义化的 LOG_LEVEL。
    level_name = (
        os.environ.get("LOG_LEVEL")
        or os.environ.get("UVICORN_LOG_LEVEL")
        or "info"
    ).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    return logger


def _new_request_id() -> str:
    """生成唯一 request_id：标准 UUID4（36 字符、含连字符）。"""
    return str(uuid.uuid4())


class RequestContextMiddleware:
    """注入 request_id、回传响应头、记录访问日志并兜底 500 的 ASGI 中间件。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = configure_access_logging()

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _new_request_id()
        token = set_request_id(request_id)
        method = scope.get("method", "-")
        path = scope.get("path", "-")
        started = time.perf_counter()
        status_code = 500  # 应用未产出响应（极端情况）时按 500 记录
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message.get("status", status_code)
                headers = message.setdefault("headers", [])
                # 异常处理器已自行写入 X-Request-ID 时不重复添加
                #（多值响应头会被部分客户端合并成逗号串）。
                already_set = any(
                    name == REQUEST_ID_HEADER.lower().encode("latin-1")
                    for name, _value in headers
                )
                if not already_set:
                    headers.append(
                        (
                            REQUEST_ID_HEADER.lower().encode("latin-1"),
                            request_id.encode("latin-1"),
                        )
                    )
            await send(message)

        error_type: str | None = None
        error_message: str | None = None
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，任何异常都统一 500
            error_type = type(exc).__name__
            error_message = str(exc)
            # 完整堆栈只进服务端日志；响应体绝不包含内部异常文本。
            self._log(
                logging.ERROR,
                "request failed with unhandled exception",
                request_id,
                method,
                path,
                500,
                started,
                error_type=error_type,
                error_message=error_message,
                exc_info=exc,
            )
            # 响应头尚未开始发送时才能回退为统一 500；流式响应开始后再出错
            # 无法替换状态行，只能交由服务端中断连接。
            if not response_started:
                response = internal_error_response()
                await response(scope, receive, send)
            else:
                raise
        else:
            level = logging.INFO if status_code < 500 else logging.ERROR
            self._log(
                level,
                "request completed",
                request_id,
                method,
                path,
                status_code,
                started,
            )
        finally:
            reset_request_id(token)

    def _log(
        self,
        level: int,
        message: str,
        request_id: str,
        method: str,
        path: str,
        status_code: int,
        started: float,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
        exc_info: Exception | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "request_id": request_id,
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        if error_type is not None:
            fields["error_type"] = error_type
        if error_message is not None:
            # 内部异常文本仅写入日志，便于凭 request_id 排障。
            fields["error_message"] = error_message
        self.logger.log(
            level, message, extra={"log_fields": fields}, exc_info=exc_info
        )
