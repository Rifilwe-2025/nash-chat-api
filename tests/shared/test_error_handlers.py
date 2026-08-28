"""Global error handling: every failure leaves as the envelope, never as a traceback."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from src.core.middleware import RequestContextMiddleware
from src.shared.exceptions import (
    ConflictException,
    NotFoundException,
    register_error_handlers,
)
from src.shared.responses import create_router


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    router = create_router(tags=["probe"])

    @router.get("/boom", summary="Raise an unexpected error")
    def boom() -> None:
        raise RuntimeError("database password is hunter2")

    @router.get("/missing", summary="Raise a not-found error")
    def missing() -> None:
        raise NotFoundException("Agent 42 does not exist")

    @router.get("/conflict", summary="Raise a conflict")
    def conflict() -> None:
        raise ConflictException()

    @router.get("/db", summary="Raise a database error")
    def database() -> None:
        raise OperationalError("SELECT secret FROM user", {}, Exception("connection refused"))

    app.include_router(router)
    return app


async def request(path: str) -> tuple[int, dict[str, Any]]:
    transport = ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    return response.status_code, response.json()


async def test_app_exception_becomes_the_envelope() -> None:
    status, body = await request("/missing")

    assert status == 404
    assert body["success"] is False
    assert body["error"] == {
        "code": "NOT_FOUND",
        "detail": "Agent 42 does not exist",
    }
    assert "value" not in body


async def test_exception_defaults_are_used_when_no_detail_is_given() -> None:
    status, body = await request("/conflict")

    assert status == 409
    assert body["error"]["code"] == "CONFLICT"
    assert body["message"] == "The request conflicts with the current state."


async def test_unexpected_error_is_generic_and_leaks_nothing() -> None:
    status, body = await request("/boom")

    assert status == 500
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["message"] == "Something went wrong."
    serialised = str(body)
    assert "hunter2" not in serialised
    assert "RuntimeError" not in serialised
    assert "Traceback" not in serialised


async def test_database_error_does_not_leak_sql() -> None:
    status, body = await request("/db")

    assert status == 500
    assert body["error"]["code"] == "DATABASE_ERROR"
    serialised = str(body)
    assert "SELECT" not in serialised
    assert "connection refused" not in serialised


async def test_unknown_route_uses_the_envelope() -> None:
    status, body = await request("/nope")

    assert status == 404
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"
