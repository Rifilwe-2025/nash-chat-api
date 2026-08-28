"""Application factory — wiring only, no business logic.

Routers are registered here; each module exposes one ``router`` from its ``presentation.api``
package. Error handlers arrive in Phase 1 alongside the ``AppException`` hierarchy.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import configs
from src.core.lifespan import lifespan
from src.core.middleware import RequestContextMiddleware
from src.modules.system.presentation.api import router as system_router


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, configs.LOGGING_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=configs.APP_NAME,
        version=configs.APP_VERSION,
        debug=configs.APP_DEBUG,
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configs.CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(system_router)

    return app
