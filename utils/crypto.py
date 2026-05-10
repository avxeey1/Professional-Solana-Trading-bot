"""
utils/crypto.py — AES-256-GCM encryption for private key storage.
Keys are NEVER stored in plaintext — only encrypted blobs in SQLite.
"""
from __future__ import annotations
import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from config import ENCRYPTION_KEY


def _get_key() -> bytes:
    key_bytes = bytes.fromhex(ENCRYPTION_KEY)
    if len(key_bytes) != 32:
        raise ValueError("ENCRYPTION_KEY must be exactly 32 bytes (64 hex chars)")
    return key_bytes


def encrypt(plaintext: str) -> str:
    """Encrypt a string → base64-encoded nonce+ciphertext."""
    aesgcm = AESGCM(_get_key())
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(blob: str) -> str:
    """Decrypt a base64-encoded nonce+ciphertext → plaintext."""
    raw = base64.b64decode(blob)
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(_get_key())
    return aesgcm.decrypt(nonce, ct, None).decode()
