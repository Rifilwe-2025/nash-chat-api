"""Pattern B connectors — API sources that are pulled and indexed (spec §5.2.1).

One implementation, configured per source, rather than a class per vendor: a product catalogue and a
help-desk article list are the same shape once you have named the endpoint, the auth, the pagination
and which fields carry the content.
"""

from src.modules.knowledge_base.internal.connectors.base import (
    AuthType,
    Connector,
    ConnectorError,
    ConnectorRecord,
    ConnectorResult,
    PaginationStyle,
)
from src.modules.knowledge_base.internal.connectors.rest import RestConnector

__all__ = [
    "AuthType",
    "Connector",
    "ConnectorError",
    "ConnectorRecord",
    "ConnectorResult",
    "PaginationStyle",
    "RestConnector",
]
