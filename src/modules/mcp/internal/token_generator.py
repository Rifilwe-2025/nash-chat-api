"""Generating and hashing a personal access token.

The shape is ``nsp_<env>_<43 url-safe characters>`` — the same reasoning as an agent API key's
``nsk_`` prefix (see ``api_keys/internal/key_generator.py``), with its own letter so a leaked
token is recognisably *a person's* credential rather than a widget's. A secret scanner, a
pre-commit hook, or somebody reading a pasted editor config can tell at a glance which kind of
thing escaped.

The environment segment is duplicated from the API key generator rather than imported, because
``internal/`` is module-private; it is two lines, and the two credentials are free to diverge.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from src import configs

PREFIX = "nsp"
# 256 bits. A token lives far longer than a session and sits in plain-text editor configuration, so
# it gets more entropy than the per-agent key rather than less.
SECRET_BYTES = 32
# Enough to tell two tokens apart in a list, far too little to help an attacker.
VISIBLE_PREFIX_LENGTH = 12


@dataclass(frozen=True, slots=True)
class GeneratedToken:
    """The only moment the secret exists. Only ``token_hash`` and ``prefix`` are ever persisted."""

    secret: str
    token_hash: str
    prefix: str


def hash_token(secret: str) -> str:
    """SHA-256: correct for a random secret, and a single indexed lookup at authentication."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def environment_segment() -> str:
    """``live`` in production, the environment's own name anywhere else."""
    env = (configs.APP_ENV or "local").strip().lower()
    return "live" if env in {"prod", "production"} else env


def generate_token() -> GeneratedToken:
    secret = f"{PREFIX}_{environment_segment()}_{secrets.token_urlsafe(SECRET_BYTES)}"
    return GeneratedToken(
        secret=secret,
        token_hash=hash_token(secret),
        prefix=secret[:VISIBLE_PREFIX_LENGTH],
    )
