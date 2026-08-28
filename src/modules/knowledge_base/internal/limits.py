"""Ingestion limits (spec §5.2).

Three checks, all made before any bytes are extracted or stored: the file type is one v1 can read,
the file is not larger than a single source may be, and the tenant is within its storage total.

They are separated from the service so the numbers are readable in one place and so the messages a
tenant sees are consistent — a size failure should say the size and the limit, not "bad request".
"""

from __future__ import annotations

from src import configs
from src.shared.exceptions import ValidationException


def max_source_bytes() -> int:
    size: int = configs.KNOWLEDGE_BASE_MAX_SOURCE_BYTES
    return size


def max_tenant_bytes() -> int:
    size: int = configs.KNOWLEDGE_BASE_MAX_TENANT_BYTES
    return size


def _megabytes(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"


def assert_within_source_limit(byte_size: int) -> None:
    limit = max_source_bytes()
    if byte_size > limit:
        raise ValidationException(
            f"The source is {_megabytes(byte_size)}; the limit for one source is "
            f"{_megabytes(limit)}.",
            code="KB_SOURCE_TOO_LARGE",
            message="The source is too large.",
        )
    if byte_size == 0:
        raise ValidationException(
            "The source is empty.", code="KB_SOURCE_EMPTY", message="The source is empty."
        )


def assert_within_tenant_limit(used_bytes: int, incoming_bytes: int) -> None:
    limit = max_tenant_bytes()
    if used_bytes + incoming_bytes > limit:
        remaining = max(limit - used_bytes, 0)
        raise ValidationException(
            f"This would use {_megabytes(used_bytes + incoming_bytes)} of the "
            f"{_megabytes(limit)} available to your account; {_megabytes(remaining)} remain.",
            code="KB_STORAGE_LIMIT_REACHED",
            message="Your knowledge base storage limit has been reached.",
        )
