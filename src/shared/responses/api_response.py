"""The response envelope every endpoint returns.

``success`` is always present; ``value`` and ``error`` are mutually exclusive. JSON keys are
camelCase — serialise with ``model_dump(by_alias=True, exclude_none=True)``.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class CamelModel(BaseModel):
    """Base for every DTO: camelCase aliases, populated by field name internally."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class ErrorDetail(CamelModel):
    code: str
    detail: str | None = None


class ApiResponse(CamelModel, Generic[T]):
    success: bool
    value: T | None = None
    error: ErrorDetail | None = None
    message: str | None = None

    @classmethod
    def ok(cls, value: T | None = None, message: str | None = None) -> ApiResponse[T]:
        return cls(success=True, value=value, message=message)

    @classmethod
    def fail(
        cls, code: str, detail: str | None = None, message: str | None = None
    ) -> ApiResponse[T]:
        return cls(success=False, error=ErrorDetail(code=code, detail=detail), message=message)


class PageMeta(CamelModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int


class PaginatedResponse(CamelModel, Generic[T]):
    success: bool
    value: list[T]
    meta: PageMeta
    message: str | None = None

    @classmethod
    def of(
        cls,
        items: list[T],
        page: int,
        page_size: int,
        total_items: int,
        message: str | None = None,
    ) -> PaginatedResponse[T]:
        total_pages = (total_items + page_size - 1) // page_size if page_size else 0
        return cls(
            success=True,
            value=items,
            meta=PageMeta(
                page=page,
                page_size=page_size,
                total_items=total_items,
                total_pages=total_pages,
            ),
            message=message,
        )
