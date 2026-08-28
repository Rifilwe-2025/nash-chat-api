"""Router factory that applies the envelope's serialisation rules.

Every module builds its router with :func:`create_router` so responses are serialised with
``by_alias=True`` and ``exclude_none=True`` without each endpoint having to remember.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute


class EnvelopeRoute(APIRoute):
    """Drops ``None`` fields so ``value``/``error`` stay mutually exclusive in the JSON.

    The flags are overwritten rather than defaulted: ``APIRouter.add_api_route`` always passes
    them explicitly, so ``setdefault`` would never take effect.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["response_model_exclude_none"] = True
        kwargs["response_model_by_alias"] = True
        super().__init__(*args, **kwargs)


def create_router(**kwargs: Any) -> APIRouter:
    kwargs.setdefault("route_class", EnvelopeRoute)
    return APIRouter(**kwargs)
