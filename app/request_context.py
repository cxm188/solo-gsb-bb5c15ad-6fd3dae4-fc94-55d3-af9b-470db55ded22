"""请求级故障关联：基于 ContextVar 的请求局部上下文。

每个 HTTP 请求在 ASGI 中间件中获得唯一 request_id 并写入 ContextVar；
同一次请求内的路由、业务校验（HTTPException）与异常处理器读到的都是
同一个 ID。Starlette / anyio 为每个连接创建独立任务并复制 contextvars
上下文，因此并发请求各自持有不同的值，互不串号。
"""

from __future__ import annotations

from contextvars import ContextVar

#: 当前请求的唯一标识；请求上下文之外读取时返回占位符 "-"。
_request_id: ContextVar[str] = ContextVar("request_id")

#: 日志中请求上下文缺失时使用的占位符。
NO_REQUEST_ID = "-"


def get_request_id() -> str:
    """返回当前请求的 request_id；不在请求上下文中时返回 "-"。"""
    try:
        return _request_id.get()
    except LookupError:
        return NO_REQUEST_ID


def set_request_id(request_id: str) -> object:
    """把 request_id 绑定到当前上下文，返回 reset 用 token（仅供中间件使用）。"""
    return _request_id.set(request_id)


def reset_request_id(token: object) -> None:
    """恢复绑定前的上下文（仅供中间件使用）。"""
    _request_id.reset(token)  # type: ignore[arg-type]
