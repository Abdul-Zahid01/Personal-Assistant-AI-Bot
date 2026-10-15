from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet


def make_fernet(secret_key: str) -> Fernet:
    """Symmetric encryption for secrets stored in the database (OAuth tokens, the Web Push key)."""
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest()))


def sign(secret_key: str, purpose: str, data: str) -> str:
    return hmac.new(secret_key.encode(), f"{purpose}:{data}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(secret_key: str, purpose: str, data: str, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign(secret_key, purpose, data), str(signature))
