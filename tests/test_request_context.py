"""请求级故障关联测试：request_id、统一错误响应、并发隔离与访问日志。"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import register_exception_handlers
from app.main import app
from app.middleware import ACCESS_LOGGER_NAME, RequestContextMiddleware
from app.request_context import get_request_id


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def captured_logs():
    """收集结构化访问日志记录（解析单行 JSON 后逐条返回）。"""
    import app.middleware as middleware

    records: list[dict] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rendered = middleware.JsonLineFormatter().format(record)
            records.append(json.loads(rendered))

    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    handler = CaptureHandler()
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _boom_app() -> FastAPI:
    """带一个必抛未捕获异常路由的最小应用（中间件 / 处理器装配与主应用一致）。"""
    boom = FastAPI()
    boom.add_middleware(RequestContextMiddleware)
    register_exception_handlers(boom)

    @boom.get("/boom")
    def boom_route() -> dict:
        raise RuntimeError("SECRET_TOKEN=must-not-appear-in-response")

    @boom.get("/rid")
    def rid_route() -> dict:
        # 同步（线程池）路由内也应读到本请求的 request_id。
        return {"rid": get_request_id()}

    return boom


# ---------- 正常响应：保持原格式并回传 X-Request-ID ----------


def test_success_body_unchanged_and_has_request_id(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 36  # UUID4 标准格式
    assert request_id.count("-") == 4

    response = client.post(
        "/api/green-wave/windows",
        json={
            "depart_from": 0,
            "depart_to": 300,
            "intersections": [
                {"cycle": 60, "offset": 0, "green_start": 0,
                 "green_end": 30, "travel": 0},
                {"cycle": 60, "offset": 30, "green_start": 0,
                 "green_end": 30, "travel": 30},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json() == [
        {"start": 0, "end": 30}, {"start": 60, "end": 90},
        {"start": 120, "end": 150}, {"start": 180, "end": 210},
        {"start": 240, "end": 270},
    ]
    assert response.headers["X-Request-ID"]


# ---------- 参数校验错误（422）统一格式 ----------


def test_business_validation_error_envelope(client):
    response = client.post(
        "/api/green-wave/windows",
        json={
            "depart_from": 0,
            "depart_to": 60,
            "intersections": [
                {"cycle": 60, "offset": 60, "green_start": 0,
                 "green_end": 10, "travel": 0}
            ],
        },
    )
    assert response.status_code == 422
    header_id = response.headers["X-Request-ID"]
    body = response.json()
    assert body["request_id"] == header_id
    assert body["code"] == "validation_error"
    assert isinstance(body["message"], str) and body["message"]
    assert body["details"] == [
        "offset 必须满足 0 <= offset < cycle（路口 cycle=60）"
    ]


def test_pydantic_validation_error_envelope(client):
    response = client.post(
        "/api/green-wave/windows",
        json={
            "depart_from": 0,
            "depart_to": 60,
            "intersections": [
                {"cycle": "abc", "offset": 0, "green_start": 0,
                 "green_end": 10, "travel": 0}
            ],
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["code"] == "validation_error"
    # 定位渲染为 字段[下标].字段 形式，错误类型给出中文说明。
    assert body["details"] == ["intersections[0].cycle: 必须是合法的整数"]


def test_missing_fields_validation_error_envelope(client):
    response = client.post("/api/green-wave/windows", json={"depart_from": 0})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert "depart_to: 字段缺失" in body["details"]
    assert "intersections: 字段缺失" in body["details"]


def test_malformed_json_validation_error_envelope(client):
    response = client.post(
        "/api/green-wave/windows",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["code"] == "validation_error"
    assert len(body["details"]) == 1
    assert body["details"][0].endswith("请求体不是合法的 JSON")
    assert body["details"][0].startswith("[")


# ---------- 业务 / 框架 HTTP 错误 ----------


def test_route_not_found_unified_envelope(client):
    response = client.get("/no-such-route")
    assert response.status_code == 404
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["code"] == "not_found"
    assert body["message"] == "请求的资源不存在"
    assert body["details"] == ["Not Found"]


def test_method_not_allowed_unified_envelope(client):
    response = client.put("/health")
    assert response.status_code == 405
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["code"] == "method_not_allowed"


# ---------- 未捕获异常：统一 500、不泄漏内部文本、有头有日志 ----------


def test_unhandled_exception_returns_safe_500_with_header(captured_logs):
    boom_client = TestClient(_boom_app(), raise_server_exceptions=False)
    response = boom_client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    header_id = response.headers["X-Request-ID"]
    assert body == {
        "request_id": header_id,
        "code": "internal_error",
        "message": "服务器内部错误，请凭 request_id 联系服务提供方排查",
        "details": [],
    }
    serialized = json.dumps(body, ensure_ascii=False)
    assert "SECRET_TOKEN" not in serialized
    assert "RuntimeError" not in serialized

    error_logs = [log for log in captured_logs if log["status_code"] == 500]
    assert len(error_logs) == 1
    log = error_logs[0]
    assert log["level"] == "error"
    assert log["request_id"] == header_id
    assert log["method"] == "GET"
    assert log["path"] == "/boom"
    assert log["error_type"] == "RuntimeError"
    # 内部异常文本与完整堆栈只进入服务端日志。
    assert "SECRET_TOKEN" in log["error_message"]
    assert "Traceback (most recent call last)" in log["traceback"]


def test_sync_route_reads_same_request_id_from_context():
    boom_client = TestClient(_boom_app())
    response = boom_client.get("/rid")
    assert response.status_code == 200
    assert response.json()["rid"] == response.headers["X-Request-ID"]


# ---------- request_id 唯一性与并发隔离 ----------


def test_request_ids_are_unique(client):
    ids = [client.get("/health").headers["X-Request-ID"] for _ in range(50)]
    assert len(set(ids)) == 50


def test_concurrent_requests_do_not_cross_context(captured_logs):
    """并发请求的响应头 / 错误体 request_id 必须各自匹配，互不串号。"""

    async def run_concurrently() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as async_client:
            tasks = []
            for index in range(40):
                if index % 4 == 0:
                    # 业务 422
                    payload = {
                        "depart_from": 0,
                        "depart_to": 60,
                        "intersections": [
                            {"cycle": 60, "offset": 60,
                             "green_start": 0, "green_end": 10, "travel": 0}
                        ],
                    }
                elif index % 4 == 1:
                    # Pydantic 422
                    payload = {
                        "depart_from": 0,
                        "depart_to": 60,
                        "intersections": [
                            {"cycle": "x", "offset": 0,
                             "green_start": 0, "green_end": 10, "travel": 0}
                        ],
                    }
                elif index % 4 == 2:
                    # 正常业务响应
                    payload = {
                        "depart_from": 0,
                        "depart_to": 60,
                        "intersections": [
                            {"cycle": 60, "offset": 0,
                             "green_start": 0, "green_end": 0, "travel": 0}
                        ],
                    }
                else:
                    # 正常空解
                    payload = {
                        "depart_from": 0,
                        "depart_to": 60,
                        "intersections": [
                            {"cycle": 60, "offset": 0,
                             "green_start": 20, "green_end": 30, "travel": 0}
                        ],
                    }
                tasks.append(
                    async_client.post("/api/green-wave/windows", json=payload)
                )
            return await asyncio.gather(*tasks)

    responses = asyncio.run(run_concurrently())
    assert len(responses) == 40
    header_ids = [response.headers["X-Request-ID"] for response in responses]
    assert len(set(header_ids)) == 40  # 每个并发请求 ID 唯一

    for response in responses:
        if response.status_code == 422:
            # 错误体中的 ID 必须与本请求响应头一致（未串到并发请求）。
            assert response.json()["request_id"] == response.headers["X-Request-ID"]
        else:
            assert response.status_code == 200

    # 每个请求恰有一条访问日志，字段齐全且 ID 与响应头一一对应。
    window_logs = [
        log for log in captured_logs if log["path"] == "/api/green-wave/windows"
    ]
    assert len(window_logs) == 40
    log_id_to_status = {log["request_id"]: log["status_code"] for log in window_logs}
    for response in responses:
        rid = response.headers["X-Request-ID"]
        assert log_id_to_status[rid] == response.status_code
    for log in window_logs:
        assert log["method"] == "POST"
        assert isinstance(log["duration_ms"], (int, float))
        assert log["duration_ms"] >= 0


# ---------- 结构化访问日志字段 ----------


def test_access_log_fields_on_success_and_error(client, captured_logs):
    client.get("/health")
    client.post(
        "/api/green-wave/windows",
        json={
            "depart_from": 0,
            "depart_to": 60,
            "intersections": [
                {"cycle": 60, "offset": 60, "green_start": 0,
                 "green_end": 10, "travel": 0}
            ],
        },
    )

    by_path = {log["path"]: log for log in captured_logs}
    health_log = by_path["/health"]
    assert health_log["level"] == "info"
    assert health_log["method"] == "GET"
    assert health_log["status_code"] == 200
    assert health_log["request_id"]
    assert "duration_ms" in health_log
    assert "timestamp" in health_log

    error_log = by_path["/api/green-wave/windows"]
    assert error_log["status_code"] == 422
    assert error_log["method"] == "POST"
    # 4xx 正常记为 info 级别（业务可预期错误），5xx 才是 error。
    assert error_log["level"] == "info"
