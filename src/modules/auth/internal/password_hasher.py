"""Password hashing (Argon2id).

Argon2 was chosen over bcrypt for memory-hardness and because it has no 72-byte input truncation.
The hash string carries its own parameters, so raising the cost later re-hashes on next login via
:meth:`PasswordHasher.needs_rehash`.
"""

from __future__ import annotations

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = Argon2Hasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-time-ish verification that never raises on a bad or missing hash."""
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True
