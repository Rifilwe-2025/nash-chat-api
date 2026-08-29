"""The REST connector — one implementation that covers most of Pattern B (spec §5.2.1).

Product catalogues, CMS articles, help-desk knowledge bases: nearly all of them are a paginated JSON
endpoint with a bearer token. Rather than a connector per vendor, this is one connector configured
per source — endpoint, auth, pagination style, and a field mapping.

**Field mapping is the interesting part.** A record is not injected as raw JSON, for the same reason
a CSV row is not (§5.2.3): `{"sku": "SKU123", "price": 45.99}` reads as noise in a prompt, while
"SKU: SKU123. Price: 45.99." reads as knowledge. ``content_fields`` name what becomes text;
everything in ``metadata_fields`` is kept for citation and filtering but stays out of the prompt.

**The same SSRF guard as URL ingestion applies.** A connector endpoint is a tenant-supplied URL that
this server fetches, so it goes through ``internal/fetching.assert_fetchable`` on every page — a
paginated response can send the next page anywhere.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from src import configs
from src.modules.knowledge_base.internal.connectors.base import (
    AuthType,
    ConnectorError,
    ConnectorRecord,
    ConnectorResult,
    PaginationStyle,
)
from src.modules.knowledge_base.internal.extractors.base import ExtractionError
from src.modules.knowledge_base.internal.fetching import assert_fetchable

DEFAULT_PAGE_SIZE = 100


def _dig(record: dict[str, Any], path: str) -> Any:
    """Read ``a.b.c`` out of a nested record. Missing anywhere yields ``None``."""
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _sentence(label: str, value: Any) -> str | None:
    """One field as a readable clause. Lists are joined; empty values are dropped entirely."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        rendered = ", ".join(str(item) for item in value if item not in (None, ""))
        return f"{label}: {rendered}." if rendered else None
    return f"{label}: {value}."


class RestConnector:
    """Pulls records from a configured JSON endpoint.

    ``config`` is the connector block stored on the source. Everything it needs is data, so a new
    integration is configuration rather than code.
    """

    def __init__(self, config: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client

    # -- configuration readers -------------------------------------------------

    @property
    def url(self) -> str:
        url = str(self.config.get("url") or "").strip()
        if not url:
            raise ConnectorError("The connector has no endpoint URL configured.")
        return url

    @property
    def records_path(self) -> str | None:
        """Where the list of records sits in the response, e.g. ``data.items``.

        ``None`` means the response body *is* the list, which is common enough to be worth
        supporting without configuration.
        """
        value = self.config.get("recordsPath")
        return str(value) if value else None

    @property
    def content_fields(self) -> list[str]:
        fields = self.config.get("contentFields") or []
        if not fields:
            raise ConnectorError(
                "The connector has no content fields configured, so there is nothing to index."
            )
        return [str(item) for item in fields]

    @property
    def metadata_fields(self) -> list[str]:
        return [str(item) for item in (self.config.get("metadataFields") or [])]

    @property
    def id_field(self) -> str:
        return str(self.config.get("idField") or "id")

    @property
    def version_field(self) -> str | None:
        value = self.config.get("versionField")
        return str(value) if value else None

    # -- the pull ----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        auth_type = AuthType(str(self.config.get("authType") or AuthType.NONE.value))
        credentials = self.config.get("credentials") or {}
        headers: dict[str, str] = {"Accept": "application/json"}

        if auth_type is AuthType.BEARER:
            token = credentials.get("token")
            if not token:
                raise ConnectorError("The connector is set to bearer auth but has no token.")
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type is AuthType.API_KEY_HEADER:
            name, value = credentials.get("header"), credentials.get("value")
            if not name or not value:
                raise ConnectorError(
                    "The connector is set to API key auth but has no header name or value."
                )
            headers[str(name)] = str(value)
        elif auth_type is AuthType.BASIC:
            username, password = credentials.get("username"), credentials.get("password")
            if not username or not password:
                raise ConnectorError(
                    "The connector is set to basic auth but has no username or password."
                )
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        return headers

    def _page_params(self, page: int, fetched: int, cursor: str | None) -> dict[str, Any]:
        style = PaginationStyle(str(self.config.get("pagination") or PaginationStyle.NONE.value))
        size = int(self.config.get("pageSize") or DEFAULT_PAGE_SIZE)

        if style is PaginationStyle.PAGE_NUMBER:
            return {
                str(self.config.get("pageParam") or "page"): page,
                str(self.config.get("pageSizeParam") or "per_page"): size,
            }
        if style is PaginationStyle.OFFSET:
            return {
                str(self.config.get("offsetParam") or "offset"): fetched,
                str(self.config.get("limitParam") or "limit"): size,
            }
        if style is PaginationStyle.CURSOR and cursor:
            return {str(self.config.get("cursorParam") or "cursor"): cursor}
        return {}

    def _next_cursor(self, payload: Any) -> str | None:
        path = self.config.get("nextCursorPath")
        if not path or not isinstance(payload, dict):
            return None
        value = _dig(payload, str(path))
        return str(value) if value else None

    def _records_from(self, payload: Any) -> list[dict[str, Any]]:
        raw = _dig(payload, self.records_path) if self.records_path else payload
        if raw is None:
            raise ConnectorError(
                f"The response has nothing at {self.records_path!r}. The API's shape may have "
                "changed."
            )
        if not isinstance(raw, list):
            raise ConnectorError("The configured records path does not point at a list.")
        return [item for item in raw if isinstance(item, dict)]

    def _to_record(self, raw: dict[str, Any]) -> ConnectorRecord | None:
        clauses = [
            sentence
            for path in self.content_fields
            if (
                sentence := _sentence(
                    path.split(".")[-1].replace("_", " ").title(), _dig(raw, path)
                )
            )
        ]
        if not clauses:
            return None  # nothing indexable in this record; skipping beats storing an empty one

        metadata = {path: _dig(raw, path) for path in self.metadata_fields}
        identifier = _dig(raw, self.id_field)
        version = _dig(raw, self.version_field) if self.version_field else None

        return ConnectorRecord(
            external_id=str(identifier) if identifier is not None else "",
            text=" ".join(clauses),
            metadata={key: value for key, value in metadata.items() if value is not None},
            version=str(version) if version is not None else None,
        )

    async def fetch(self) -> ConnectorResult:
        """Walk the endpoint's pages and return every record it yields."""
        # Configuration is checked before anything is sent. A connector with no content fields
        # would fetch the whole catalogue and then discover it has nothing to index — the tenant
        # should be told what is wrong without their endpoint being hit at all.
        self._assert_configured()

        owned = self._client is None
        http = self._client or httpx.AsyncClient(
            timeout=configs.KNOWLEDGE_BASE_URL_FETCH_TIMEOUT_SECONDS, follow_redirects=False
        )

        max_pages: int = configs.SYNC_MAX_PAGES
        max_records: int = configs.SYNC_MAX_RECORDS
        records: list[ConnectorRecord] = []
        cursor: str | None = None
        pages = 0
        truncated = False

        try:
            while pages < max_pages:
                # Re-checked on every page: a paginated API can hand back a next page anywhere.
                self._assert_fetchable(self.url)
                payload = await self._get(http, self._page_params(pages + 1, len(records), cursor))
                pages += 1

                page_records = self._records_from(payload)
                if not page_records:
                    break

                for raw in page_records:
                    record = self._to_record(raw)
                    if record is not None:
                        records.append(record)
                    if len(records) >= max_records:
                        truncated = True
                        break

                if truncated:
                    break

                cursor = self._next_cursor(payload)
                style = PaginationStyle(
                    str(self.config.get("pagination") or PaginationStyle.NONE.value)
                )
                if style is PaginationStyle.NONE:
                    break
                if style is PaginationStyle.CURSOR and not cursor:
                    break
            else:
                truncated = True

            return ConnectorResult(records=records, pages_fetched=pages, truncated=truncated)
        finally:
            if owned:
                await http.aclose()

    def _assert_configured(self) -> None:
        """Raise on anything wrong with the configuration itself, before any request."""
        _ = self.url, self.content_fields
        self._headers()

    def _assert_fetchable(self, url: str) -> None:
        try:
            assert_fetchable(url)
        except ExtractionError as exc:
            raise ConnectorError(str(exc)) from exc

    async def _get(self, http: httpx.AsyncClient, params: dict[str, Any]) -> Any:
        try:
            response = await http.get(self.url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ConnectorError(
                f"The endpoint could not be reached: {type(exc).__name__}."
            ) from exc

        if response.status_code in (401, 403):
            raise ConnectorError(
                f"The endpoint rejected the configured credentials (HTTP "
                f"{response.status_code}). They may have expired."
            )
        if response.status_code >= 400:
            raise ConnectorError(f"The endpoint responded with HTTP {response.status_code}.")

        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError("The endpoint did not return JSON.") from exc
