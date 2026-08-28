"""OpenAPI / Swagger metadata.

The interactive docs are a deliverable of this API, not a by-product: the integration
documentation handed to tenants is generated from this schema. Every module adds its tag here with
a one-line description of what the tag covers.
"""

from __future__ import annotations

from typing import Any

API_DESCRIPTION = """
Backend for a multi-tenant platform for building, configuring, and deploying custom AI chat agents.

**Response envelope** — every endpoint returns the same shape, with camelCase keys.
`value` and `error` are mutually exclusive, and null fields are omitted:

```json
{ "success": true, "value": { }, "message": null }
```

**Errors** carry a stable `error.code` alongside a human-readable `error.detail`.

**Request tracing** — every response carries an `X-Request-ID` header. Send your own to have it
echoed back, and quote it when reporting a problem.
"""

TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "system",
        "description": "Service health and readiness probes.",
    },
]
