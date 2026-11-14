"""Per-user Blablador API keys.

Each user supplies their own key, so GPU time meters against their own account
rather than a shared service credential.

The key has to be stored, not just held in the session: jobs run for hours and
the reconciler must poll and download results long after the user has gone
home. It is encrypted at rest with Fernet, and the encryption key lives in the
environment rather than the database, so a database dump alone does not reveal
anyone's credential.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

log = logging.getLogger(__name__)


class CredentialError(RuntimeError):
    pass


def _fernet() -> Fernet:
    raw = settings.CREDENTIAL_ENCRYPTION_KEY
    if not raw:
        if not settings.DEBUG:
            raise CredentialError(
                "CREDENTIAL_ENCRYPTION_KEY is not set; refusing to store user "
                "credentials with a derived development key."
            )
        # Development convenience only, and never in production (above).
        raw = base64.urlsafe_b64encode(
            hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        ).decode()
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except (ValueError, TypeError) as exc:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_KEY must be a urlsafe base64-encoded 32-byte "
            "key, as produced by Fernet.generate_key()."
        ) from exc


def encrypt(api_key: str) -> str:
    return _fernet().encrypt(api_key.strip().encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Usually means CREDENTIAL_ENCRYPTION_KEY was rotated or differs
        # between the web and reconciler processes.
        raise CredentialError(
            "stored credential could not be decrypted with the current key"
        ) from exc


def fingerprint(api_key: str) -> str:
    """A short, non-reversible label so a user can tell which key is stored."""
    digest = hashlib.sha256(api_key.strip().encode()).hexdigest()
    return digest[:8]
