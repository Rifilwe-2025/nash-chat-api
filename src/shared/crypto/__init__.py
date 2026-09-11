"""Encryption at rest for the secrets the platform holds on a tenant's behalf (spec §5.7)."""

from src.shared.crypto.cipher import (
    EncryptionError,
    decrypt,
    encrypt,
    encryption_enabled,
    generate_key,
    warn_if_unprotected,
)
from src.shared.crypto.copies import open_copy, seal_copy
from src.shared.crypto.types import EncryptedJson, EncryptedString

__all__ = [
    "EncryptedJson",
    "EncryptedString",
    "EncryptionError",
    "decrypt",
    "encrypt",
    "encryption_enabled",
    "generate_key",
    "open_copy",
    "seal_copy",
    "warn_if_unprotected",
]
