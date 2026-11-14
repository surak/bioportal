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
audit = logging.getLogger("jobs.audit")


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

    def get_or_create_user(self, access_token, id_token, payload):
        """Refuse a deactivated account.

        mozilla-django-oidc returns a matched user without consulting
        user_can_authenticate(), so without this an administrator who
        deactivates someone in the Django admin would not actually stop them
        signing in with their federated identity. Sirtfi [OS4] requires that
        access can be terminated.
        """
        user = super().get_or_create_user(access_token, id_token, payload)
        if user is not None and not self.user_can_authenticate(user):
            log.warning("refused sign-in for deactivated user %s", user.pk)
            audit.warning("login-refused user=%s reason=inactive", user.username)
            return None
        return user

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
