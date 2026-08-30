"""Who may read the operator metrics.

Everything else in this module is tenant data, reached with the caller's own access token. The
process metrics are not: they describe the deployment, across every tenant, so a tenant token is
exactly the wrong credential for them and there is no admin role in v1 to hold the right one.

So they are gated on a shared secret the operator configures, sent as ``X-Operator-Token``. With no
token configured the endpoint is **closed**, not open — a deployment that has not thought about this
must not be publishing its request counts to whoever asks. Comparison is constant-time; a plain
``==`` on a secret leaks its prefix through timing.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header

from src import configs
from src.shared.exceptions import ForbiddenException, ServiceUnavailableException


def require_operator(
    token: Annotated[
        str | None,
        Header(
            alias="X-Operator-Token",
            description="The operator secret configured as `OBSERVABILITY_OPERATOR_TOKEN`.",
        ),
    ] = None,
) -> None:
    configured: str = (configs.OBSERVABILITY_OPERATOR_TOKEN or "").strip()
    if not configured:
        raise ServiceUnavailableException(
            "Operator metrics are not enabled on this deployment. Set "
            "OBSERVABILITY_OPERATOR_TOKEN to turn them on.",
            code="METRICS_DISABLED",
        )
    if not token or not hmac.compare_digest(token, configured):
        raise ForbiddenException(
            "A valid X-Operator-Token header is required.", code="OPERATOR_TOKEN_INVALID"
        )


OperatorDep = Annotated[None, Depends(require_operator)]
