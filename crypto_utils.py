"""Symmetric encryption of private keys using Fernet (AES-128-CBC + HMAC)."""
from __future__ import annotations

from cryptography.fernet import Fernet

from config import require_fernet_key


def _cipher() -> Fernet:
    return Fernet(require_fernet_key().encode())


def encrypt(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _cipher().decrypt(ciphertext.encode()).decode()
