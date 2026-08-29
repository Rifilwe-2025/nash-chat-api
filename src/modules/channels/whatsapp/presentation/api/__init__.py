"""The WhatsApp channel's two router halves.

They are separate routers rather than one because they have nothing in common but a tag: the
connection routes are authenticated by a user token and live under ``/agents/…``, while the webhook
is public, lives under ``/v1/channels/whatsapp/…``, and is called only by Meta. Merging them would
mean one router whose prefix and dependencies are true for half its routes.
"""

from src.modules.channels.whatsapp.presentation.api.connection_controller import (
    router as connection_router,
)
from src.modules.channels.whatsapp.presentation.api.webhook_controller import (
    router as webhook_router,
)

__all__ = ["connection_router", "webhook_router"]
