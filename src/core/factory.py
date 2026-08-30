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
from src.core.openapi import API_DESCRIPTION, TAGS_METADATA
from src.core.security_headers import SecurityHeadersMiddleware
from src.modules.agents.presentation.api import router as agents_router
from src.modules.analytics.presentation.api import router as analytics_router
from src.modules.api_keys.presentation.api import router as api_keys_router
from src.modules.auth.presentation.api import router as auth_router
from src.modules.billing.presentation.api import router as billing_router
from src.modules.channels.presentation.api import router as channels_router
from src.modules.channels.web.presentation.api import router as web_chat_router
from src.modules.channels.whatsapp.presentation.api import (
    connection_router as whatsapp_connection_router,
)
from src.modules.channels.whatsapp.presentation.api import (
    webhook_router as whatsapp_webhook_router,
)
from src.modules.conversations.presentation.api import router as conversations_router
from src.modules.knowledge_base.presentation.api import router as knowledge_base_router
from src.modules.system.presentation.api import router as system_router
from src.modules.tenants.presentation.api import router as account_router
from src.modules.tools.presentation.api import router as tools_router
from src.shared.exceptions import register_error_handlers


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, configs.LOGGING_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )


def create_app() -> FastAPI:
    configure_logging()

    docs_enabled = configs.DOCS_ENABLED

    app = FastAPI(
        title=configs.APP_NAME,
        version=configs.APP_VERSION,
        description=API_DESCRIPTION,
        summary="Build, configure and deploy custom AI chat agents.",
        openapi_tags=TAGS_METADATA,
        debug=configs.APP_DEBUG,
        lifespan=lifespan,
        docs_url=configs.DOCS_SWAGGER_PATH if docs_enabled else None,
        redoc_url=configs.DOCS_REDOC_PATH if docs_enabled else None,
        openapi_url=configs.DOCS_OPENAPI_PATH if docs_enabled else None,
        swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True},
    )

    # Outermost, so the headers are on every response — including the ones the error handlers
    # produce and the ones the CORS middleware answers by itself.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configs.CORS_ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)

    app.include_router(system_router)
    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(agents_router)
    app.include_router(knowledge_base_router)
    app.include_router(conversations_router)
    app.include_router(tools_router)
    app.include_router(api_keys_router)
    app.include_router(analytics_router)
    app.include_router(billing_router)
    # WhatsApp before the generic channel router: `PUT /agents/{id}/channels/{channel_type}` also
    # matches `/agents/{id}/channels/whatsapp`, and FastAPI takes the first route that matches. The
    # generic route refuses WhatsApp anyway, so a future reordering fails loudly rather than
    # quietly storing a connection with no credentials.
    app.include_router(whatsapp_connection_router)
    app.include_router(whatsapp_webhook_router)
    app.include_router(channels_router)
    app.include_router(web_chat_router)

    return app
