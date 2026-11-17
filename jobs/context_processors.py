"""Template-wide facts about how a visitor can sign in.

Every template that offers a way in needs to know which ways exist. Without a
registered AAI client, Helmholtz AAI rejects the authorize request with
"Missing client_id parameter", so that link has to be absent rather than dead;
a local account then stands in for it.
"""

from __future__ import annotations

from django.conf import settings


def auth_capabilities(request) -> dict:
    return {
        "oidc_available": bool(getattr(settings, "OIDC_AVAILABLE", False)),
        "allow_self_signup": bool(getattr(settings, "ALLOW_SELF_SIGNUP", False)),
    }
