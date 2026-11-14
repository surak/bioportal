"""Check the configured OIDC endpoints against the provider's own metadata.

    manage.py checkoidc

Endpoints cannot be derived from the issuer URL by pattern: Helmholtz AAI
serves authorization from a different path prefix, and its JWKS is at /jwk
rather than /jwks. This command compares what the portal is configured with
against the provider's discovery document, and reports the redirect URIs that
must be registered for this deployment.
"""

from __future__ import annotations

import json
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand
from django.urls import reverse

CHECKS = [
    ("authorization_endpoint", "OIDC_OP_AUTHORIZATION_ENDPOINT"),
    ("token_endpoint", "OIDC_OP_TOKEN_ENDPOINT"),
    ("userinfo_endpoint", "OIDC_OP_USER_ENDPOINT"),
    ("jwks_uri", "OIDC_OP_JWKS_ENDPOINT"),
]


class Command(BaseCommand):
    help = "Compare configured OIDC endpoints with the provider's discovery document."

    def handle(self, *args, **opts):
        issuer = settings.OIDC_ISSUER if hasattr(settings, "OIDC_ISSUER") else None
        issuer = issuer or settings.OIDC_OP_TOKEN_ENDPOINT.rsplit("/", 1)[0]
        url = f"{issuer}/.well-known/openid-configuration"
        self.stdout.write(f"discovery: {url}")

        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                meta = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"could not fetch discovery: {exc}"))
            return

        problems = 0
        for key, setting in CHECKS:
            expected = meta.get(key)
            actual = getattr(settings, setting, None)
            if expected and actual != expected:
                problems += 1
                self.stdout.write(self.style.ERROR(f"  MISMATCH {setting}"))
                self.stdout.write(f"    configured: {actual}")
                self.stdout.write(f"    provider:   {expected}")
            else:
                self.stdout.write(self.style.SUCCESS(f"  ok {setting}"))

        wanted = set(settings.OIDC_RP_SCOPES.split())
        offered = set(meta.get("scopes_supported") or [])
        missing = wanted - offered
        if missing:
            problems += 1
            self.stdout.write(
                self.style.ERROR(f"  scopes not offered by provider: {sorted(missing)}")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"  ok scopes: {sorted(wanted)}"))

        algs = meta.get("id_token_signing_alg_values_supported") or []
        if settings.OIDC_RP_SIGN_ALGO not in algs:
            problems += 1
            self.stdout.write(
                self.style.ERROR(
                    f"  signing algorithm {settings.OIDC_RP_SIGN_ALGO} not in {algs}"
                )
            )

        base = settings.SITE_BASE_URL.rstrip("/")
        self.stdout.write("\nregister these redirect URIs for this deployment:")
        for name in ("oidc_authentication_callback", "oidc_logout"):
            self.stdout.write(f"  {base}{reverse(name)}")

        self.stdout.write(
            "" if problems else self.style.SUCCESS("\nconfiguration matches the provider")
        )
        if problems:
            self.stdout.write(self.style.ERROR(f"\n{problems} problem(s) found"))
