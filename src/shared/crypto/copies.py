"""Encrypted copies of the credentials this platform issues, so their owner can copy them again.

API keys and personal access tokens are **authenticated by a SHA-256 hash**, and that does not
change: a presented secret is hashed and looked up, and nothing on that path reads a copy. What this
adds is a second, optional column holding the secret under AES-256-GCM, opened only when the owner
asks the console to copy it.

**No encryption key, no copy.** :func:`~src.shared.crypto.cipher.encrypt` passes a value through in
clear when ``SECURITY_ENCRYPTION_KEY`` is unset. That is a defensible default for a credential a
tenant hands us, and an indefensible one for a credential whose whole design is that the database
never holds it readable. So :func:`seal_copy` stores nothing at all without a key, and the
credential is simply not copyable — its owner saw it once, when it was issued.

**Revoking discards the copy.** A revoked credential never works again, so there is nothing worth
copying and no reason to keep a readable form of it.
"""

from __future__ import annotations

from src.shared.crypto.cipher import PREFIX, EncryptionError, decrypt, encrypt, encryption_enabled


def seal_copy(secret: str) -> str | None:
    """The secret encrypted for later copying, or ``None`` when no key allows storing it."""
    if not encryption_enabled():
        return None
    return encrypt(secret)


def open_copy(sealed: str) -> str:
    """Decrypt a stored copy. A value that is not an envelope is refused, never returned as is."""
    if not sealed.startswith(PREFIX):
        raise EncryptionError("A stored credential copy is not encrypted; refusing to return it.")
    return decrypt(sealed)
