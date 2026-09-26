"""统一错误响应。

三类错误统一为同一种 JSON 结构（HTTP 状态码保持语义不变）：

- 参数校验错误（Pydantic 字段校验与 ``validate_ranges`` 抛出的 422
  HTTPException）：``422``、错误码 ``validation_error``；
- 业务 HTTP 错误（其余 HTTPException 及框架 404/405 等）：保留原状态码，
  错误码按状态码映射；
- 未捕获异常：``500``、错误码 ``internal_error``，响应只含通用提示，
  不暴露内部异常文本（完整堆栈仅写入服务端日志）。

所有错误响应都携带 ``X-Request-ID`` 响应头，与正常响应一致。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .request_context import get_request_id

#: 统一错误响应的响应头名称。
REQUEST_ID_HEADER = "X-Request-ID"

#: 常见 HTTP 状态码到错误码的映射；未列出的状态码使用 http_<code>。
HTTP_STATUS_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
    504: "gateway_timeout",
}

#: Pydantic v2 常见校验错误类型到中文说明的映射。
_VALIDATION_TYPE_MESSAGES: dict[str, str] = {
    "missing": "字段缺失",
    "missing_field": "字段缺失",
    "extra_forbidden": "不允许传入额外字段",
    "int_type": "必须是整数",
    "int_parsing": "必须是合法的整数",
    "float_type": "必须是数字",
    "float_parsing": "必须是合法的数字",
    "bool_type": "必须是布尔值 true/false",
    "bool_parsing": "必须是合法的布尔值",
    "string_type": "必须是字符串",
    "string_too_short": "字符串长度小于下限",
    "string_too_long": "字符串长度超过上限",
    "list_type": "必须是数组",
    "list_too_short": "数组元素数量少于下限",
    "list_too_long": "数组元素数量超过上限",
    "dict_type": "必须是对象",
    "model_type": "必须是对象",
    "greater_than": "必须大于允许值",
    "greater_than_equal": "不能小于允许的下限值",
    "less_than": "必须小于允许值",
    "less_than_equal": "不能大于允许的上限值",
    "value_error": "取值不合法",
    "json_invalid": "请求体不是合法的 JSON",
    "json_type": "请求体必须是 JSON",
    "enum": "取值不在允许的范围内",
    "literal_error": "取值不在允许的范围内",
    "url_parsing": "必须是合法的 URL",
    "uuid_parsing": "必须是合法的 UUID",
    "date_parsing": "必须是合法的日期",
    "time_parsing": "必须是合法的时间",
    "datetime_parsing": "必须是合法的日期时间",
}

#: 未捕获异常返回给客户端的通用提示（不含任何内部异常信息）。
INTERNAL_ERROR_MESSAGE = "服务器内部错误，请凭 request_id 联系服务提供方排查"

#: 框架自带 HTTP 错误（Starlette 默认英文短语）的中文可读信息。
FRAMEWORK_STATUS_MESSAGES: dict[int, str] = {
    400: "错误的请求",
    404: "请求的资源不存在",
    405: "不支持该 HTTP 方法",
    413: "请求体过大",
    414: "请求 URI 过长",
    415: "不支持的媒体类型",
}


def error_code_for_status(status_code: int) -> str:
    """把 HTTP 状态码映射为稳定的机器可读错误码。"""
    return HTTP_STATUS_ERROR_CODES.get(status_code, f"http_{status_code}")


def _error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: list[str] | None = None,
) -> JSONResponse:
    """构造统一错误 JSON 响应并附带 X-Request-ID 响应头。"""
    body: dict[str, Any] = {
        "request_id": request_id,
        "code": code,
        "message": message,
        "details": details or [],
    }
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={REQUEST_ID_HEADER: request_id},
    )


def _format_location(loc: tuple[Any, ...]) -> str:
    """把 Pydantic 错误定位（如 ("body", "intersections", 0, "cycle")）渲染成可读路径。"""
    parts = list(loc)
    # 首段固定是 body / query / path，对调用方没有信息量，去掉。
    if parts and isinstance(parts[0], str) and parts[0] in ("body", "query", "path"):
        parts = parts[1:]
    rendered = ""
    for part in parts:
        if isinstance(part, int):
            rendered = f"{rendered}[{part}]" if rendered else f"[{part}]"
        elif rendered:
            rendered = f"{rendered}.{part}"
        else:
            rendered = str(part)
    return rendered


def _format_validation_detail(error: dict[str, Any]) -> str:
    """把单条 Pydantic 校验错误渲染成一行中文可读信息。"""
    location = _format_location(tuple(error.get("loc", ())))
    type_code = error.get("type", "")
    # 按完整错误类型精确映射，查不到时回退到 Pydantic 原始英文信息。
    message = _VALIDATION_TYPE_MESSAGES.get(
        type_code, error.get("msg", "参数不合法")
    )
    if location:
        return f"{location}: {message}"
    return message


def _normalize_detail(detail: Any) -> list[str]:
    """把 HTTPException.detail 规整为字符串列表（业务错误目前均为 str / list[str]）。"""
    if detail is None:
        return []
    if isinstance(detail, list):
        return [str(item) for item in detail]
    return [str(detail)]


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """处理业务 HTTP 错误与框架 HTTP 错误（404/405 等），统一错误 JSON。"""
    request_id = get_request_id()
    status_code = exc.status_code
    code = error_code_for_status(status_code)
    details = _normalize_detail(exc.detail)
    if status_code == 422:
        code = "validation_error"
        message = "请求参数校验失败"
    else:
        message = "请求处理失败"
        # 框架默认英文短语（如 404 "Not Found"）本地化为中文；
        # 业务方自定义 detail 则原样作为可读信息。
        localized = FRAMEWORK_STATUS_MESSAGES.get(status_code)
        if localized is not None and isinstance(exc.detail, str):
            message = localized
        elif len(details) == 1 and isinstance(exc.detail, str):
            message = exc.detail
    return _error_response(
        status_code, code, message, request_id, details=details
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """处理 Pydantic 请求体 / 参数校验错误，统一错误 JSON。"""
    request_id = get_request_id()
    details = [_format_validation_detail(error) for error in exc.errors()]
    return _error_response(
        422,
        "validation_error",
        "请求参数校验失败",
        request_id,
        details=details,
    )


def internal_error_response() -> JSONResponse:
    """未捕获异常的统一 500 响应：不回传内部异常文本。"""
    return _error_response(
        500,
        "internal_error",
        INTERNAL_ERROR_MESSAGE,
        get_request_id(),
    )


def register_exception_handlers(app: Any) -> None:
    """把统一错误处理器注册到 FastAPI 应用。

    未捕获异常（500）由请求中间件在 ASGI 层兜底捕获（处理器在最外层
    ServerErrorMiddleware 中运行、读不到请求上下文），此处不注册
    通用 Exception 处理器。
    """
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
