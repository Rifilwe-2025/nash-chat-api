"""Encryption at rest for tenant-provided credentials (spec §5.7).

Several tables hold secrets that belong to a tenant and that the platform has to be able to *use*,
not merely verify: a WhatsApp access token, the API key for a tool's endpoint. Unlike a password or
one of our own API keys, those cannot be hashed — we have to present them to somebody else. Until
this phase they sat in JSON columns in clear, which the models said plainly rather than pretending
otherwise. This is where that gets fixed.

**AES-256-GCM.** Authenticated encryption, so a ciphertext that has been tampered with fails to
decrypt rather than decrypting to something else. A fresh 96-bit nonce per encryption, generated
from the OS: reusing a nonce under GCM is catastrophic, not merely weak, and the only safe way to
avoid it is never to derive one from anything.

**Stored as ``v1:<base64(nonce || ciphertext)>``.** The version prefix is not decoration — it is
what makes the next key rotation or algorithm change a readable ``if`` rather than a guess about
what a blob of base64 once was.

**No key configured means no encryption, loudly.** Local development and the test suite run without
a key, and a platform that invented one at startup would produce data nobody could decrypt after a
restart. So :func:`encrypt` returns the value unchanged, :func:`decrypt` passes through anything
that is not a recognised envelope, and the application logs a warning at startup outside local
environments. Being plain about an unencrypted column is better than the appearance of protection.

Rotation, when it comes, has a shape already: add ``v2`` with the new key, decrypt either version,
encrypt only the new one, and every row re-encrypts itself the next time it is written.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src import configs

logger = logging.getLogger("api.crypto")

PREFIX = "v1:"
NONCE_BYTES = 12
KEY_BYTES = 32


class EncryptionError(RuntimeError):
    """A key is present but unusable, or a stored value will not decrypt under it.

    Deliberately not an ``AppException``: neither case is something a caller did, and neither should
    be reported to them as a routine failure. A wrong key is a deployment fault that must surface
    loudly rather than quietly return an empty credential — which would look, from the outside,
    exactly like a tenant who never configured one.
    """


def generate_key() -> str:
    """A fresh base64 key, for whoever is setting ``SECURITY_ENCRYPTION_KEY`` up."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def _key() -> bytes | None:
    """The configured key, or ``None`` when encryption is off.

    Read on each call rather than cached at import: the test suite changes configuration through
    the environment and reloads, and a key captured at import time would ignore that.
    """
    raw: str = (configs.SECURITY_ENCRYPTION_KEY or "").strip()
    if not raw:
        return None

    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EncryptionError(
            "SECURITY_ENCRYPTION_KEY is not valid base64. Generate one with "
            '`python -c "from src.shared.crypto import generate_key; print(generate_key())"`.'
        ) from exc

    if len(key) != KEY_BYTES:
        raise EncryptionError(
            f"SECURITY_ENCRYPTION_KEY must decode to {KEY_BYTES} bytes, got {len(key)}."
        )
    return key


def encryption_enabled() -> bool:
    return _key() is not None


def encrypt(plaintext: str) -> str:
    """Encrypt, or return the value unchanged when no key is configured."""
    key = _key()
    if key is None:
        return plaintext

    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return PREFIX + base64.b64encode(nonce + sealed).decode()


def decrypt(stored: str) -> str:
    """Decrypt an envelope; anything else is returned as it was.

    The pass-through is what makes turning encryption on a configuration change rather than a data
    migration: rows written before the key was set are still readable, and each re-encrypts itself
    the next time it is written.
    """
    if not stored.startswith(PREFIX):
        return stored

    key = _key()
    if key is None:
        raise EncryptionError(
            "A stored value is encrypted but no SECURITY_ENCRYPTION_KEY is configured. "
            "Restore the key that wrote it — the data cannot be recovered without it."
        )

    try:
        raw = base64.b64decode(stored[len(PREFIX) :], validate=True)
        return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], None).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError) as exc:
        raise EncryptionError(
            "A stored credential could not be decrypted with the configured key. It was written "
            "with a different key, or the row has been altered."
        ) from exc


def warn_if_unprotected() -> None:
    """Say so at startup when secrets are being written in clear outside local development."""
    if encryption_enabled():
        return
    if configs.APP_ENV in {"local", "test"}:
        return
    logger.warning(
        "SECURITY_ENCRYPTION_KEY is not set in the %r environment — tenant credentials "
        "(WhatsApp tokens, tool API keys) are being stored unencrypted (spec §5.7).",
        configs.APP_ENV,
    )
