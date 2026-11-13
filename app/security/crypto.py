"""Encryption at rest (Fernet) and keyed hashing (HMAC-SHA256)."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class DecryptionError(Exception):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(get_settings().data_encryption_key.encode())


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet().decrypt(token)
    except InvalidToken as exc:
        raise DecryptionError("Unable to decrypt (wrong DATA_ENCRYPTION_KEY or corrupted data)") from exc


def encrypt_json(obj: Any) -> str:
    return encrypt_bytes(json.dumps(obj, separators=(",", ":")).encode()).decode()


def decrypt_json(token: str | None) -> Any:
    if not token:
        return None
    return json.loads(decrypt_bytes(token.encode()))


def _subkey(purpose: str) -> bytes:
    return hmac.new(get_settings().secret_key.encode(), f"ironpulse:{purpose}".encode(), hashlib.sha256).digest()


def hmac_hex(value: str, purpose: str) -> str:
    """Keyed hash for low-entropy secrets (OTP codes) - never store those in plain text."""
    return hmac.new(_subkey(purpose), value.encode(), hashlib.sha256).hexdigest()


def pepper_hash(value: str) -> str:
    """Peppered hash of an ID-card number (normalised) for duplicate-ID fraud detection."""
    norm = "".join(ch for ch in value.upper() if ch.isalnum())
    return hmac.new(get_settings().id_hash_pepper.encode(), norm.encode(), hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    """SHA-256 of a high-entropy random token (sessions, e-mail links)."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def equals(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode(), b.encode())
