"""
Encryption utility for sensitive data (stream keys).

Uses Fernet symmetric encryption with a key derived from environment.
"""
import os
import base64
import logging
from cryptography.fernet import Fernet


logger = logging.getLogger(__name__)


# Encryption key from environment (generate if not set)
# In production, this should be set securely
_DEFAULT_KEY = base64.urlsafe_b64encode(b"youtube-agent-default-key-32-bytes!!").decode()


def get_encryption_key() -> bytes:
    """Get encryption key from environment or use default."""
    key = os.getenv("STREAM_ENCRYPTION_KEY", _DEFAULT_KEY)
    # Ensure key is valid Fernet key (44 bytes base64)
    if isinstance(key, str):
        key = key.encode()
    if len(key) != 44:
        # Derive a proper key from the input
        import hashlib
        key_bytes = hashlib.sha256(key).digest()
        key = base64.urlsafe_b64encode(key_bytes)
    return key


def encrypt(text: str) -> str:
    """
    Encrypt text using Fernet symmetric encryption.

    Args:
        text: Plain text to encrypt

    Returns:
        Encrypted text (base64 encoded)
    """
    if not text:
        return ""
    key = get_encryption_key()
    f = Fernet(key)
    encrypted = f.encrypt(text.encode())
    return encrypted.decode()


def decrypt(encrypted_text: str) -> str:
    """
    Decrypt text using Fernet symmetric encryption.

    Args:
        encrypted_text: Encrypted text (base64 encoded)

    Returns:
        Decrypted plain text

    Raises:
        ValueError: If decryption fails
    """
    if not encrypted_text:
        return ""
    try:
        key = get_encryption_key()
        f = Fernet(key)
        decrypted = f.decrypt(encrypted_text.encode())
        return decrypted.decode()
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        raise ValueError("Failed to decrypt stream key")
