"""Pagination primitives shared by every repository and router."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

from fastapi import Depends, Query

T = TypeVar("T")

MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class PageRequest:
    page: int = 1
    page_size: int = 20

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """A slice of rows plus the total matching count, before serialisation."""

    items: list[T]
    total: int
    page: int
    page_size: int


def page_params(
    page: Annotated[int, Query(ge=1, description="1-indexed page number.")] = 1,
    page_size: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_SIZE, description=f"Rows per page (max {MAX_PAGE_SIZE})."),
    ] = 20,
) -> PageRequest:
    return PageRequest(page=page, page_size=page_size)


PageParamsDep = Annotated[PageRequest, Depends(page_params)]
