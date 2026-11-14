"""JSC / Helmholtz AAI login.

Users authenticate against the institute IdP; the local Django password login
stays available for the administrator account only.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

log = logging.getLogger(__name__)


class JscOIDCBackend(OIDCAuthenticationBackend):
    def verify_claims(self, claims) -> bool:
        if not super().verify_claims(claims):
            return False
        allowed = settings.OIDC_ALLOWED_ENTITLEMENTS
        if not allowed:
            return True
        held = set(claims.get("eduperson_entitlement") or [])
        held |= set(claims.get("groups") or [])
        if held & set(allowed):
            return True
        log.warning("denying login for %s: no matching entitlement", claims.get("sub"))
        raise PermissionDenied("Your account is not entitled to use this service.")

    def create_user(self, claims):
        user = super().create_user(claims)
        return self._apply_claims(user, claims)

    def update_user(self, user, claims):
        return self._apply_claims(user, claims)

    def _apply_claims(self, user, claims):
        user.email = claims.get("email", user.email) or ""
        user.first_name = claims.get("given_name", "") or ""
        user.last_name = claims.get("family_name", "") or ""
        # A federated login never confers admin rights; those are granted
        # locally in the Django admin.
        user.save(update_fields=["email", "first_name", "last_name"])
        return user
