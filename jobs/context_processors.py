"""Template-wide facts about how a visitor can sign in.

Every template that offers "Sign in with JSC" needs to know whether federated
sign-in is registered yet. Without a client, Helmholtz AAI rejects the
authorize request with "Missing client_id parameter", so the link has to be
absent rather than dead.
"""

from __future__ import annotations

from django.conf import settings


def auth_capabilities(request) -> dict:
    return {
        "oidc_available": bool(getattr(settings, "OIDC_AVAILABLE", False)),
    }
