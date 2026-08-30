"""A per-client throttle on the credential endpoints (spec §5.7, Phase 13 hardening).

The per-key limit added in Phase 8 protects the chat API, and it protects it *per key* — which by
definition cannot cover the endpoints where no key exists yet. Sign-up, login, and refresh are the
three doors a caller can knock on without any credential at all, and they are the ones worth
counting: password guessing, refresh-token probing, and sign-up floods all arrive here.

**Counted per client address, before any work is done.** That is a blunt instrument and it is
chosen knowingly: several people behind one office NAT share a bucket. The limit is set well above
what a person does by hand and well below what a script does, so the shared-address case costs
nobody anything real, while a burst of attempts stops early — and it stops *before* a password hash
is computed, which is the expensive part of a login and the reason an unthrottled login endpoint is
a denial-of-service vector as much as a guessing one.

The address is read from the ASGI client rather than from ``X-Forwarded-For``. A header any caller
can set is not an identity: trusting it would let one client present a fresh address per request and
defeat the limit entirely. Deployments behind a proxy should run uvicorn with ``--proxy-headers``
and ``--forwarded-allow-ips`` pointed at the proxy, which resolves the real client into ``scope``
where this reads it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from src import configs
from src.core.rate_limit import RateLimiter, build_limiter
from src.shared.exceptions import RateLimitedException


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:  # pragma: no cover - lifespan always sets this
        limiter = build_limiter()
        request.app.state.rate_limiter = limiter
    return limiter


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def client_address(request: Request) -> str:
    """The connecting address, or a shared bucket when there is none.

    A request with no client — an in-process ASGI call, some test transports — falls into one
    ``unknown`` bucket rather than being exempted. Exempting it would make the limit disappear in
    exactly the deployment shape where it is hardest to notice.
    """
    return request.client.host if request.client else "unknown"


async def throttle_credentials(request: Request, limiter: RateLimiterDep) -> None:
    """Count this attempt against the caller's address and refuse once it is over."""
    limit: int = configs.RATE_LIMIT_AUTH_PER_MINUTE
    verdict = await limiter.check(f"auth:{client_address(request)}", limit)
    request.state.rate_limit_headers = verdict.headers()

    if not verdict.allowed:
        raise RateLimitedException(
            f"Too many authentication attempts. Retry in {verdict.retry_after} seconds.",
            code="RATE_LIMITED",
        )


CredentialThrottleDep = Annotated[None, Depends(throttle_credentials)]
