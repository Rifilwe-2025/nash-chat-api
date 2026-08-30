"""Column types that encrypt themselves (spec §5.7).

The alternative was to encrypt in each service that touches a credential. That fails the first time
someone adds a column and forgets, and the failure is silent — a plaintext secret in a JSONB column
looks exactly like an encrypted one to everybody except an attacker with a database dump. Putting it
in the column type means a model declares *what the data is* and protection follows, in one place,
for every path that reads or writes it: services, tasks, and the worker alike.

The trade is that these columns can no longer be queried by their contents. That costs nothing here:
credentials are looked up by the row that owns them and never searched, and a column you can search
is a column an index leaks.

Two types, because the two shapes have genuinely different failure modes.

* :class:`EncryptedJson` holds a mapping — a tool's ``authConfig``, a channel's credentials. It is
  serialised, encrypted, and stored as a JSON *string*, which is still valid JSONB, so the column
  needs no migration and a database client sees an opaque quoted blob rather than field names.
* :class:`EncryptedString` holds one value, such as a webhook signing secret.

Both pass through anything that is not a recognised envelope on read, so switching encryption on is
a configuration change rather than a data migration (see :mod:`src.shared.crypto.cipher`).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from src.shared.crypto.cipher import decrypt, encrypt


class EncryptedJson(TypeDecorator[dict[str, Any]]):
    """A JSONB column whose contents are encrypted at rest.

    ``cache_ok`` is true because the type carries no per-instance state — the key comes from
    configuration at call time, not from the type — so SQLAlchemy may safely reuse a compiled
    statement built with it.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        # An empty mapping stays an empty mapping: "no credential configured" is not a secret, and
        # storing an encrypted "{}" would make every unconfigured row look like a configured one.
        if not value:
            return {}
        return encrypt(json.dumps(value, separators=(",", ":"), sort_keys=True))

    def process_result_value(self, value: Any, dialect: Dialect) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            # Written before encryption was turned on. Readable as it stands, and re-encrypted the
            # next time the row is written.
            return value
        decrypted = decrypt(str(value))
        loaded: Any = json.loads(decrypted)
        return loaded if isinstance(loaded, dict) else {}


class EncryptedString(TypeDecorator[str]):
    """A text column whose contents are encrypted at rest."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None or value == "":
            return value
        return encrypt(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None or value == "":
            return value
        return decrypt(value)
