"""Generating and hashing an API key (spec §5.6).

The shape is ``nsk_<env>_<32 url-safe characters>``. The prefix is not decoration:

* ``nsk_`` makes a leaked key greppable. Secret scanners and pre-commit hooks find credentials by
  recognisable prefixes, and a bare random string is invisible to all of them.
* the environment segment stops the classic mistake of pointing staging at a live key and only
  finding out from the invoice.

Entropy comes from ``secrets.token_urlsafe(24)`` — 192 bits, well past anything guessable, and
URL-safe so a key survives being pasted into a query string by someone who should not have.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from src import configs

PREFIX = "nsk"
SECRET_BYTES = 24
# Enough to tell two keys apart in a list, far too little to help an attacker.
VISIBLE_PREFIX_LENGTH = 12


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """The only moment the secret exists. Only ``key_hash`` and ``prefix`` are ever persisted."""

    secret: str
    key_hash: str
    prefix: str


def hash_key(secret: str) -> str:
    """SHA-256, matching how auth tokens are stored.

    Correct for a random secret: no work factor is needed against 192 bits of entropy, and
    authentication must be a single indexed lookup rather than a scan-and-verify over every row.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def environment_segment() -> str:
    """``live`` in production, the environment's own name anywhere else."""
    env = (configs.APP_ENV or "local").strip().lower()
    return "live" if env in {"prod", "production"} else env


def generate_key() -> GeneratedKey:
    secret = f"{PREFIX}_{environment_segment()}_{secrets.token_urlsafe(SECRET_BYTES)}"
    return GeneratedKey(
        secret=secret,
        key_hash=hash_key(secret),
        prefix=secret[:VISIBLE_PREFIX_LENGTH],
    )
