"""Application exception hierarchy.

Services raise these; the global handlers in :mod:`src.shared.exceptions.error_handlers` turn them
into the response envelope. Each carries a stable ``code`` that clients can branch on — the string
is part of the API contract, so document it in the route's ``responses={...}``.
"""

from __future__ import annotations

from http import HTTPStatus


class AppException(Exception):
    """Base for every expected failure. Never leaks internals to the client."""

    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"
    message: str = "Something went wrong."

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        self.detail = detail
        if code is not None:
            self.code = code
        if message is not None:
            self.message = message
        super().__init__(self.detail or self.message)


class BadRequestException(AppException):
    status_code = HTTPStatus.BAD_REQUEST
    code = "BAD_REQUEST"
    message = "The request could not be processed."


class ValidationException(AppException):
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "VALIDATION_ERROR"
    message = "The request payload failed validation."


class UnauthorizedException(AppException):
    status_code = HTTPStatus.UNAUTHORIZED
    code = "UNAUTHORIZED"
    message = "Authentication is required."


class ForbiddenException(AppException):
    status_code = HTTPStatus.FORBIDDEN
    code = "FORBIDDEN"
    message = "You do not have access to this resource."


class NotFoundException(AppException):
    status_code = HTTPStatus.NOT_FOUND
    code = "NOT_FOUND"
    message = "The requested resource does not exist."


class ConflictException(AppException):
    status_code = HTTPStatus.CONFLICT
    code = "CONFLICT"
    message = "The request conflicts with the current state."


class PlanLimitException(AppException):
    """The tenant's plan does not allow this (spec §5.9).

    ``402 Payment Required`` rather than 403 or 429, and the distinction is one an integration acts
    on: 403 means *this credential may never do this*, 429 means *try again shortly*, and neither is
    true here. The request is legitimate, the caller is permitted, and waiting will not help — the
    account needs a bigger plan. 402 is the status every billing-aware client already treats that
    way.
    """

    status_code = HTTPStatus.PAYMENT_REQUIRED
    code = "PLAN_LIMIT_EXCEEDED"
    message = "Your plan's limit has been reached."


class RateLimitedException(AppException):
    status_code = HTTPStatus.TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    message = "Too many requests."


class ServiceUnavailableException(AppException):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    code = "SERVICE_UNAVAILABLE"
    message = "A dependency is unavailable."
