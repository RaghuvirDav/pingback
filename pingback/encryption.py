from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from pingback.config import ENCRYPTION_KEY

logger = logging.getLogger("pingback.encryption")

_fernet: Fernet | None = None


def _get_fernet() -> Fernet | None:
    global _fernet
    if _fernet is not None:
        return _fernet
    if not ENCRYPTION_KEY:
        logger.warning("ENCRYPTION_KEY not set — sensitive fields stored in plaintext")
        return None
    _fernet = Fernet(ENCRYPTION_KEY.encode())
    return _fernet


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string value. Returns ciphertext or the original if no key configured."""
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a string value. Falls back to returning the raw value if decryption fails
    (handles pre-encryption plaintext data gracefully)."""
    f = _get_fernet()
    if f is None:
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except (InvalidToken, Exception):
        # Value was stored before encryption was enabled — return as-is
        return ciphertext
