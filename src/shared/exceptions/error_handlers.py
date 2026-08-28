"""Global exception handlers.

Every failure leaves as the same envelope. Unexpected exceptions are logged in full server-side with
the request id, and the client gets a generic message — no stack traces, no driver text, no SQL.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.shared.exceptions.exceptions import AppException
from src.shared.responses import ApiResponse

logger = logging.getLogger("api.error")


def _request_id(request: Request) -> str | None:
    value = request.scope.get("state", {}).get("request_id")
    return str(value) if value else None


def _envelope(
    status_code: int,
    code: str,
    message: str,
    detail: str | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    body = ApiResponse[Any].fail(code=code, detail=detail, message=message)
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(by_alias=True, exclude_none=True),
        headers=headers,
    )


async def handle_app_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppException)
    logger.info(
        "%s %s -> %s (%s)",
        request.method,
        request.url.path,
        exc.code,
        exc.detail or exc.message,
    )
    return _envelope(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        detail=exc.detail,
        request_id=_request_id(request),
    )


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}"
        for error in exc.errors()
    )
    return _envelope(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message="The request payload failed validation.",
        detail=fields or None,
        request_id=_request_id(request),
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = HTTPStatus(exc.status_code)
    return _envelope(
        status_code=exc.status_code,
        code=status.name,
        message=status.phrase,
        detail=str(exc.detail) if exc.detail else None,
        request_id=_request_id(request),
    )


async def handle_database_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("database error on %s %s", request.method, request.url.path, exc_info=exc)
    return _envelope(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="DATABASE_ERROR",
        message="A database error occurred.",
        request_id=_request_id(request),
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return _envelope(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="Something went wrong.",
        request_id=_request_id(request),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppException, handle_app_exception)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(SQLAlchemyError, handle_database_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
