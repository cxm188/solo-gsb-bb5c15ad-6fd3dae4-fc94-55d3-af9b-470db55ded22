"""request_id 中间件、统一错误响应与结构化访问日志的端到端测试。

不依赖 httpx/TestClient，直接用原生 ASGI 调用驱动应用。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.main import app as real_app
from app.observability import (
    REQUEST_ID_HEADER,
    get_request_id,
    install_observability,
)


# ---------------------------------------------------------------------------
# 原生 ASGI 测试工具
# ---------------------------------------------------------------------------


async def _asgi_call(application, method, path, raw_body=None, headers=None):
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": raw_body or b"",
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    messages = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in (headers or [])
        ],
    }
    await application(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    response_headers = {
        key.decode(): value.decode() for key, value in start["headers"]
    }
    return start["status"], response_headers, body


def run(coro):
    return asyncio.run(coro)


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def app_logs():
    handler = _ListHandler()
    loggers = [logging.getLogger("app.access"), logging.getLogger("app.error")]
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        for logger in loggers:
            logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# 一个用于制造 500 / 并发场景的最小应用
# ---------------------------------------------------------------------------


class _Item(BaseModel):
    count: int = Field(..., ge=1)


def _build_probe_app():
    application = FastAPI()

    @application.get("/ok")
    async def ok():
        return {"status": "ok"}

    @application.get("/whoami")
    async def whoami():
        # 主动让出事件循环，制造并发交错机会
        await asyncio.sleep(0.02)
        return {"request_id": get_request_id()}

    @application.get("/boom")
    def boom():
        raise RuntimeError("SECRET-INTERNAL-MARKER 不得出现在响应中")

    @application.post("/validate")
    def validate(item: _Item):
        return {"count": item.count}

    @application.post("/business-error")
    def business_error():
        raise HTTPException(status_code=422, detail=["跨字段校验失败原因"])

    install_observability(application)
    return application


probe_app = _build_probe_app()


# ---------------------------------------------------------------------------
# request_id 响应头
# ---------------------------------------------------------------------------


def test_health_carries_request_id_header():
    status, headers, body = run(_asgi_call(real_app, "GET", "/health"))
    assert status == 200
    assert REQUEST_ID_HEADER.lower() in headers
    request_id = headers[REQUEST_ID_HEADER.lower()]
    assert len(request_id) == 36  # UUID4 字符串
    assert json.loads(body) == {"status": "ok"}  # 正常业务响应体格式不变


def test_success_body_format_unchanged():
    payload = json.dumps(
        {
            "depart_from": 0,
            "depart_to": 60,
            "intersections": [
                {"cycle": 60, "offset": 0, "green_start": 0, "green_end": 30,
                 "travel": 0}
            ],
        }
    ).encode()
    status, headers, body = run(
        _asgi_call(
            real_app, "POST", "/api/green-wave/windows",
            payload, [("Content-Type", "application/json")],
        )
    )
    assert status == 200
    assert json.loads(body) == [{"start": 0, "end": 30}]
    assert REQUEST_ID_HEADER.lower() in headers


def test_request_ids_are_unique_per_request():
    _, headers_a, _ = run(_asgi_call(real_app, "GET", "/health"))
    _, headers_b, _ = run(_asgi_call(real_app, "GET", "/health"))
    id_a = headers_a[REQUEST_ID_HEADER.lower()]
    id_b = headers_b[REQUEST_ID_HEADER.lower()]
    assert id_a and id_b and id_a != id_b


# ---------------------------------------------------------------------------
# 统一错误响应
# ---------------------------------------------------------------------------


def test_pydantic_validation_error_envelope():
    # 缺少必填字段 intersections，Pydantic 422
    payload = b'{"depart_from": 0, "depart_to": 60}'
    status, headers, body = run(
        _asgi_call(
            real_app, "POST", "/api/green-wave/windows",
            payload, [("Content-Type", "application/json")],
        )
    )
    assert status == 422
    data = json.loads(body)
    request_id = headers[REQUEST_ID_HEADER.lower()]
    assert data["request_id"] == request_id
    assert data["error"]["code"] == "validation_error"
    assert data["error"]["message"]
    assert isinstance(data["error"]["details"], list)
    assert data["error"]["details"][0]["field"] == "intersections"
    assert data["error"]["details"][0]["message"]


def test_nested_field_path_is_flattened():
    # cycle=0 命中字段级约束；loc 形如 (body, intersections, 0, cycle)
    payload = json.dumps(
        {
            "depart_from": 0,
            "depart_to": 60,
            "intersections": [
                {"cycle": 0, "offset": 0, "green_start": 0, "green_end": 30,
                 "travel": 0}
            ],
        }
    ).encode()
    status, _, body = run(
        _asgi_call(
            real_app, "POST", "/api/green-wave/windows",
            payload, [("Content-Type", "application/json")],
        )
    )
    assert status == 422
    details = json.loads(body)["error"]["details"]
    fields = {item["field"] for item in details}
    assert "intersections[0].cycle" in fields


def test_malformed_json_is_validation_error():
    status, headers, body = run(
        _asgi_call(
            real_app, "POST", "/api/green-wave/windows",
            b"{not valid json", [("Content-Type", "application/json")],
        )
    )
    assert status == 422
    data = json.loads(body)
    assert data["request_id"] == headers[REQUEST_ID_HEADER.lower()]
    assert data["error"]["code"] == "validation_error"


def test_business_http_error_keeps_readable_reasons():
    # 跨字段校验：offset 越界，schemas 层抛 HTTPException(422, detail=[...])
    payload = json.dumps(
        {
            "depart_from": 0,
            "depart_to": 60,
            "intersections": [
                {"cycle": 60, "offset": 60, "green_start": 0, "green_end": 30,
                 "travel": 0}
            ],
        }
    ).encode()
    status, headers, body = run(
        _asgi_call(
            real_app, "POST", "/api/green-wave/windows",
            payload, [("Content-Type", "application/json")],
        )
    )
    assert status == 422
    data = json.loads(body)
    assert data["request_id"] == headers[REQUEST_ID_HEADER.lower()]
    assert data["error"]["code"] == "validation_error"
    assert any("offset" in reason for reason in data["error"]["details"])


def test_business_http_error_on_probe_app():
    status, headers, body = run(_asgi_call(probe_app, "POST", "/business-error"))
    assert status == 422
    data = json.loads(body)
    assert data["request_id"] == headers[REQUEST_ID_HEADER.lower()]
    assert data["error"]["code"] == "validation_error"
    assert data["error"]["details"] == ["跨字段校验失败原因"]


def test_404_uses_unified_envelope_and_header():
    status, headers, body = run(_asgi_call(real_app, "GET", "/no-such-path"))
    assert status == 404
    data = json.loads(body)
    assert data["request_id"] == headers[REQUEST_ID_HEADER.lower()]
    assert data["error"]["code"] == "not_found"
    assert data["error"]["message"]


def test_405_uses_unified_envelope_and_header():
    status, headers, body = run(_asgi_call(real_app, "DELETE", "/health"))
    assert status == 405
    data = json.loads(body)
    assert data["request_id"] == headers[REQUEST_ID_HEADER.lower()]
    assert data["error"]["code"] == "method_not_allowed"


def test_uncaught_exception_returns_generic_500_without_internal_text():
    status, headers, body = run(_asgi_call(probe_app, "GET", "/boom"))
    assert status == 500
    data = json.loads(body)
    assert data["request_id"] == headers[REQUEST_ID_HEADER.lower()]
    assert data["error"]["code"] == "internal_error"
    assert data["error"]["message"]
    assert "SECRET-INTERNAL-MARKER" not in body.decode()
    assert "RuntimeError" not in body.decode()
    # 500 不回传任何内部细节
    assert "details" not in data["error"]


# ---------------------------------------------------------------------------
# 并发隔离
# ---------------------------------------------------------------------------


def test_concurrent_requests_do_not_share_request_ids():
    async def scenario():
        results = await asyncio.gather(
            *[_asgi_call(probe_app, "GET", "/whoami") for _ in range(20)]
        )
        return results

    results = run(scenario())
    header_ids = []
    body_ids = []
    for status, headers, body in results:
        assert status == 200
        header_ids.append(headers[REQUEST_ID_HEADER.lower()])
        body_ids.append(json.loads(body)["request_id"])
    assert len(set(header_ids)) == 20  # 每请求唯一
    assert header_ids == body_ids       # 路由上下文与响应头同一 ID，无串号


# ---------------------------------------------------------------------------
# 结构化访问日志
# ---------------------------------------------------------------------------


def test_access_log_on_success(app_logs):
    run(_asgi_call(real_app, "GET", "/health"))
    access = [r for r in app_logs if r.name == "app.access"]
    assert len(access) == 1
    record = access[0]
    assert record.method == "GET"
    assert record.path == "/health"
    assert record.status_code == 200
    assert isinstance(record.duration_ms, float)
    assert record.duration_ms >= 0
    assert len(record.request_id) == 36


def test_access_log_on_validation_error(app_logs):
    payload = b'{"depart_from": 0}'
    run(
        _asgi_call(
            real_app, "POST", "/api/green-wave/windows",
            payload, [("Content-Type", "application/json")],
        )
    )
    access = [r for r in app_logs if r.name == "app.access"]
    assert len(access) == 1
    assert access[0].status_code == 422
    assert access[0].request_id
    assert access[0].levelno == logging.WARNING


def test_access_log_and_error_log_on_uncaught_exception(app_logs):
    run(_asgi_call(probe_app, "GET", "/boom"))
    access = [r for r in app_logs if r.name == "app.access"]
    errors = [r for r in app_logs if r.name == "app.error"]
    assert len(access) == 1 and access[0].status_code == 500
    assert access[0].levelno == logging.ERROR
    assert len(errors) == 1
    # 完整堆栈只在服务端日志中
    assert "SECRET-INTERNAL-MARKER" in (errors[0].exc_text or "") or errors[0].exc_info
    # 异常日志也带请求级关联字段
    assert errors[0].request_id == access[0].request_id


def test_log_records_serialize_as_json_with_required_fields(app_logs):
    from app.observability import _JsonFormatter

    run(_asgi_call(real_app, "GET", "/health"))
    formatter = _JsonFormatter()
    line = json.loads(formatter.format(app_logs[0]))
    for field in (
        "timestamp", "level", "request_id", "method", "path",
        "status_code", "duration_ms",
    ):
        assert field in line
    assert line["level"] == "info"
    assert line["path"] == "/health"
