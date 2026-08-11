import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AES_256_KEY_SIZE = 32
_NONCE_SIZE = 12
_AUTH_TAG_SIZE = 16


def _require_key(key: bytes) -> None:
    if len(key) != _AES_256_KEY_SIZE:
        raise ValueError("AES-GCM key must be 32 bytes")


def seal(data: bytes, key: bytes) -> bytes:
    """Seal bytes with AES-256-GCM and prefix a fresh 96-bit nonce."""
    _require_key(key)
    nonce = os.urandom(_NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def unseal(token: bytes, key: bytes) -> bytes:
    """Authenticate and decrypt a nonce-prefixed AES-256-GCM token."""
    _require_key(key)
    if len(token) < _NONCE_SIZE + _AUTH_TAG_SIZE:
        raise ValueError("sealed token is too short")
    nonce = token[:_NONCE_SIZE]
    ciphertext = token[_NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ciphertext, None)
