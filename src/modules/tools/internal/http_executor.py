"""Making the outbound call (spec §5.2.1).

The one place a tool actually reaches the internet, and the only place tenant credentials are ever
attached to a request. Everything about it is shaped by the fact that **a customer is waiting** and
**the arguments came from a language model**.

*Credentials are injected here and nowhere else.* They come off ``auth_config_json`` at the moment
of sending and go into headers that are never returned, never logged, and never rendered into the
result the model reads. That is the whole security promise of Pattern A: the tenant's API key stays
server-side, so an agent cannot be talked into disclosing a credential it was never given.

*Timeouts are short and the retry policy is narrow.* One retry, and only for a connection error or a
5xx — the failures where trying again is genuinely likely to work. A 4xx is the tenant's API saying
no, and asking again just spends another second of someone's patience. A timeout is not retried at
all: the customer has already waited the full budget once.

*Failures are values, not exceptions.* :class:`ToolResponse` carries the outcome, so the caller can
record it and hand the model a graceful sentence instead of failing the turn — the "I couldn't check
that right now" the spec asks for.

*Nothing is followed.* Redirects are disabled rather than re-checked hop by hop: a tool endpoint is
a fixed API a tenant configured, and an API that answers a lookup with a redirect to somewhere else
is not behaving in a way worth accommodating on a path this sensitive.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src import configs
from src.modules.tools.domain.models import AgentTool, HttpMethod, ToolAuthType, ToolOutcome
from src.modules.tools.internal import allowlist

logger = logging.getLogger("api.tools")

# Retried once. Anything else either will not be fixed by repeating it, or has already spent the
# caller's whole patience budget.
RETRYABLE_STATUS = frozenset({502, 503, 504})

# auth_config_json keys
HEADER_NAME = "headerName"
VALUE = "value"
USERNAME = "username"
PASSWORD = "password"

DEFAULT_HEADER_NAME = "X-API-Key"


@dataclass(frozen=True, slots=True)
class ToolResponse:
    """One execution, as the caller sees it. Never raises; failure is a field."""

    outcome: ToolOutcome
    payload: Any = None
    status_code: int | None = None
    duration_ms: int = 0
    error_detail: str | None = None
    url: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in (ToolOutcome.SUCCEEDED, ToolOutcome.CACHED)


def auth_headers(tool: AgentTool) -> dict[str, str]:
    """The credential headers for one tool.

    Built fresh per call and never stored on the tool object, so there is no instance holding a
    decrypted secret for longer than the request that needs it.
    """
    config = tool.auth_config_json or {}
    secret = str(config.get(VALUE) or "")

    if tool.auth_type is ToolAuthType.API_KEY_HEADER:
        name = str(config.get(HEADER_NAME) or DEFAULT_HEADER_NAME)
        return {name: secret} if secret else {}

    if tool.auth_type is ToolAuthType.BEARER:
        return {"Authorization": f"Bearer {secret}"} if secret else {}

    if tool.auth_type is ToolAuthType.BASIC:
        username = str(config.get(USERNAME) or "")
        password = str(config.get(PASSWORD) or "")
        if not username and not password:
            return {}
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    return {}


def timeout_for(tool: AgentTool) -> float:
    configured = tool.timeout_seconds
    return float(configured) if configured and configured > 0 else configs.TOOLS_TIMEOUT_SECONDS


async def execute(
    tool: AgentTool,
    arguments: dict[str, Any],
    allowed_hosts: list[str],
    client: httpx.AsyncClient | None = None,
) -> ToolResponse:
    """Run one tool call. Returns an outcome rather than raising, whatever happens."""
    started = time.perf_counter()

    try:
        url = allowlist.resolve_url(tool.endpoint_url, arguments, allowed_hosts)
    except allowlist.ToolSecurityError as exc:
        # Refused before anything left the process. Logged at warning because a tool trying to
        # reach somewhere it may not is worth someone's attention.
        logger.warning("tool %s refused: %s", tool.id, exc)
        return ToolResponse(
            outcome=ToolOutcome.REFUSED,
            error_detail=str(exc)[:500],
            duration_ms=_elapsed(started),
            arguments=arguments,
        )

    # Arguments that filled a path placeholder are not repeated in the query or body — they have
    # already been used, and sending them twice makes the request say something the tenant's API
    # did not expect.
    consumed = set(allowlist.path_placeholders(tool.endpoint_url))
    payload = {key: value for key, value in arguments.items() if key not in consumed}

    owned = client is None
    http = client or httpx.AsyncClient(
        timeout=timeout_for(tool),
        follow_redirects=False,
        headers={"User-Agent": configs.TOOLS_USER_AGENT},
    )

    try:
        return await _send(http, tool, url, payload, arguments, started)
    finally:
        if owned:
            await http.aclose()


async def _send(
    http: httpx.AsyncClient,
    tool: AgentTool,
    url: str,
    payload: dict[str, Any],
    arguments: dict[str, Any],
    started: float,
) -> ToolResponse:
    method = tool.http_method.value.upper()
    headers = {"Accept": "application/json", **auth_headers(tool)}
    attempts = 1 + max(0, configs.TOOLS_MAX_RETRIES)

    last: ToolResponse | None = None
    for attempt in range(attempts):
        try:
            if tool.http_method is HttpMethod.GET:
                response = await http.request(method, url, params=payload, headers=headers)
            else:
                response = await http.request(method, url, json=payload, headers=headers)
        except httpx.TimeoutException:
            # Not retried: the customer has already waited the whole budget once, and a second full
            # timeout would double a wait that is already too long.
            return ToolResponse(
                outcome=ToolOutcome.TIMED_OUT,
                error_detail=f"The call timed out after {timeout_for(tool)} seconds.",
                duration_ms=_elapsed(started),
                url=url,
                arguments=arguments,
            )
        except httpx.HTTPError as exc:
            last = ToolResponse(
                outcome=ToolOutcome.FAILED,
                error_detail=f"The endpoint could not be reached: {type(exc).__name__}.",
                duration_ms=_elapsed(started),
                url=url,
                arguments=arguments,
            )
            if attempt + 1 < attempts:
                continue
            return last

        if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
            continue

        if response.status_code >= 400:
            return ToolResponse(
                outcome=ToolOutcome.FAILED,
                status_code=response.status_code,
                error_detail=_failure_detail(response),
                duration_ms=_elapsed(started),
                url=url,
                arguments=arguments,
            )

        return ToolResponse(
            outcome=ToolOutcome.SUCCEEDED,
            payload=_body(response),
            status_code=response.status_code,
            duration_ms=_elapsed(started),
            url=url,
            arguments=arguments,
        )

    return last or ToolResponse(
        outcome=ToolOutcome.FAILED,
        error_detail="The call did not complete.",
        duration_ms=_elapsed(started),
        url=url,
        arguments=arguments,
    )


def _body(response: httpx.Response) -> Any:
    """Parse JSON where the endpoint sent it, and fall back to text where it did not.

    A tenant's API that answers ``text/plain`` is still usable — the mapper renders a scalar — so a
    non-JSON body is a shape to handle rather than a failure.
    """
    try:
        return response.json()
    except ValueError:
        return response.text


def _failure_detail(response: httpx.Response) -> str:
    """A short, safe description of a 4xx/5xx.

    The body is included because a tenant debugging their own API needs its error message, and
    capped hard because that body is not something we control the size of.
    """
    body = response.text.strip().replace("\n", " ")
    if not body:
        return f"The endpoint responded with HTTP {response.status_code}."
    return f"HTTP {response.status_code}: {body[:300]}"


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
