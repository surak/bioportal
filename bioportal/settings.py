from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Secrets live outside the repo. The Blablador key is read from the environment
# at call time, never stored in the database or committed.
load_dotenv(os.environ.get("BIOPORTAL_ENV_FILE", BASE_DIR / ".env"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = _bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in _env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in _env("DJANGO_CSRF_ORIGINS", "").split(",") if o]

SITE_BASE_URL = _env("SITE_BASE_URL", "http://localhost:8000")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mozilla_django_oidc",
    "jobs",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "bioportal.urls"
WSGI_APPLICATION = "bioportal.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": (
        {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _env("DB_NAME", "bioportal"),
            "USER": _env("DB_USER", "bioportal"),
            "PASSWORD": _env("DB_PASSWORD", ""),
            "HOST": _env("DB_HOST", "localhost"),
            "PORT": _env("DB_PORT", "5432"),
        }
        if _env("DB_HOST")
        else {
            # Development and container smoke tests only; production sets DB_HOST.
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _env("DB_SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        }
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{v}"}
    for v in (
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    )
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication -------------------------------------------------------
# Two doors: Helmholtz AAI / JSC Unity for users, and Django's own local
# password login for the administrator.
AUTHENTICATION_BACKENDS = [
    "jobs.auth.JscOIDCBackend",
    "django.contrib.auth.backends.ModelBackend",
]

OIDC_RP_CLIENT_ID = _env("OIDC_CLIENT_ID")
OIDC_RP_CLIENT_SECRET = _env("OIDC_CLIENT_SECRET")
OIDC_RP_SIGN_ALGO = "RS256"

# Taken from the provider's discovery document, not derived from the issuer:
# two of these do not follow the pattern you would guess. Verify with
# `manage.py checkoidc` if the provider ever moves them.
_OIDC_ISSUER = _env("OIDC_ISSUER", "https://login.helmholtz.de/oauth2")
_OIDC_HOST = _OIDC_ISSUER.rsplit("/", 1)[0]
OIDC_OP_AUTHORIZATION_ENDPOINT = _env(
    "OIDC_AUTH_ENDPOINT", f"{_OIDC_HOST}/oauth2-as/oauth2-authz"
)
OIDC_OP_TOKEN_ENDPOINT = _env("OIDC_TOKEN_ENDPOINT", f"{_OIDC_ISSUER}/token")
OIDC_OP_USER_ENDPOINT = _env("OIDC_USER_ENDPOINT", f"{_OIDC_ISSUER}/userinfo")
OIDC_OP_JWKS_ENDPOINT = _env("OIDC_JWKS_ENDPOINT", f"{_OIDC_ISSUER}/jwk")
OIDC_RP_SCOPES = _env("OIDC_SCOPES", "openid email profile eduperson_entitlement")

# Only members of these entitlements/groups may log in. Empty = allow any
# successful OIDC login (fine for a pilot, tighten before opening it up).
OIDC_ALLOWED_ENTITLEMENTS = [
    e for e in _env("OIDC_ALLOWED_ENTITLEMENTS", "").split(",") if e.strip()
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "jobs:index"
LOGOUT_REDIRECT_URL = "jobs:index"

# --- Blablador molecular shim ---------------------------------------------
# The key is held only by this backend. Direct-from-browser calls and CORS were
# not validated in that deployment, so every request is proxied through here.
BLABLADOR_API_BASE = _env("BLABLADOR_API_BASE", "https://api.blablador.fz-juelich.de/v1")

# There is no service-wide API key: each user stores their own, so GPU time is
# metered to them. Stored keys are encrypted with this Fernet key, which lives
# in the environment and never in the database.
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CREDENTIAL_ENCRYPTION_KEY = _env("CREDENTIAL_ENCRYPTION_KEY", "")

# Results are deleted upstream 48h after a job reaches a terminal state, so the
# reconciler copies them here as soon as a job completes.
ARTIFACT_ROOT = _env("ARTIFACT_ROOT", str(BASE_DIR / "artifacts"))
ARTIFACT_RETENTION_SECONDS = int(_env("ARTIFACT_RETENTION_SECONDS", "172800"))
# Upstream caps a ZIP at 2 GB; cap what we are willing to store locally too.
MAX_ARTIFACT_BYTES = int(_env("MAX_ARTIFACT_BYTES", str(2_000_000_000)))

# A job still "running" after this long has probably lost its worker: there is
# no lease-based recovery upstream, so we flag it for operations.
STALE_RUNNING_SECONDS = int(_env("STALE_RUNNING_SECONDS", str(30 * 3600)))

# --- Email ----------------------------------------------------------------
EMAIL_BACKEND = _env(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = _env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(_env("EMAIL_PORT", "25"))
EMAIL_USE_TLS = _bool("EMAIL_USE_TLS", False)
DEFAULT_FROM_EMAIL = _env("DEFAULT_FROM_EMAIL", "bioportal@fz-juelich.de")

# --- Quotas ---------------------------------------------------------------
MAX_ACTIVE_JOBS_PER_USER = int(_env("MAX_ACTIVE_JOBS_PER_USER", "5"))

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO")},
}
