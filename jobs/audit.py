"""Authentication audit trail.

Sirtfi [TR1] requires that "who, what, where and when" can be answered during
a security incident, and Helmholtz AAI requires the service security contact
to be able to provide information about user logins. Django logs neither
successful nor failed authentication by default, so these signals do.

Records carry the username, the client address and a timestamp. They never
carry credentials: no API key, password or token is written here.
"""

from __future__ import annotations

import logging

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

audit = logging.getLogger("jobs.audit")


def client_ip(request) -> str:
    """The client address, honouring the proxy header the ingress sets."""
    if request is None:
        return "-"
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        # Left-most entry is the original client.
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "-")


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    audit.info(
        "login user=%s id=%s ip=%s", user.get_username(), user.pk, client_ip(request)
    )


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        audit.info(
            "logout user=%s id=%s ip=%s",
            user.get_username(), user.pk, client_ip(request),
        )


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    # `credentials` is already masked by Django, but take only the username.
    audit.warning(
        "login-failed user=%s ip=%s",
        credentials.get("username", "-"), client_ip(request),
    )
