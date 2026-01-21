"""
Django settings for alliancehub.

Design goals:
- This file should not change between development, test, and production.
- Environment variables (.env) provide all environment-specific configuration.
- If moving servers requires editing this file, that is usually a sign that a value
  should be moved to the environment.
"""

from __future__ import annotations

import os
from pathlib import Path


# ---------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------
# These helpers keep parsing consistent and avoid repeated boilerplate.


def env_str(key: str, default: str | None = None) -> str | None:
    """Read a string env var. Empty strings are treated as missing."""
    val = os.environ.get(key)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


def env_bool(key: str, default: bool = False) -> bool:
    """
    Read a boolean env var.
    True values: 1, true, yes, y, t, on (case-insensitive)
    """
    val = env_str(key)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "y", "t", "on"}


def env_int(key: str, default: int) -> int:
    """Read an integer env var; falls back to default if parsing fails."""
    val = env_str(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def env_list_csv(key: str, default: list[str] | None = None) -> list[str]:
    """
    Read a comma-separated env var into a list.
    Example: "a,b,c" -> ["a", "b", "c"]
    """
    raw = env_str(key)
    if not raw:
        return default or []
    return [x.strip() for x in raw.split(",") if x.strip()]


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# Core environment flags
# ---------------------------------------------------------------------
# DEBUG should be enabled only for local development.
DEBUG = env_bool("DEBUG", False)

# SECRET_KEY must be unique per environment and kept secret in test/prod.
SECRET_KEY = env_str("SECRET_KEY")

# ALLOWED_HOSTS controls which Host headers Django will accept.
# For Docker/local dev, include localhost + 127.0.0.1 + 0.0.0.0.
ALLOWED_HOSTS = env_list_csv("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# CSRF_TRUSTED_ORIGINS controls which origins are trusted for browser-based POSTs
# when using cookies (CSRF protection). This typically matches your site URL(s).
CSRF_TRUSTED_ORIGINS = env_list_csv("CSRF_TRUSTED_ORIGINS", default=[])


# ---------------------------------------------------------------------
# Reverse proxy / HTTPS behavior
# ---------------------------------------------------------------------
# When running behind a reverse proxy (Caddy/Nginx), enable forwarded host/proto
# handling so Django can correctly detect HTTPS and build absolute URLs.
USE_X_FORWARDED_HOST = env_bool("USE_X_FORWARDED_HOST", False)

# If enabled, Django considers a request secure when the proxy sets:
#   X-Forwarded-Proto: https
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if env_bool("SECURE_PROXY_SSL_HEADER", False)
    else None
)

# Optional HTTPS hardening toggles. These are typically enabled in test/prod.
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", False)

# HSTS tells browsers to prefer HTTPS for a period of time.
# Start with a low value on test, then raise in production after validation.
SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", False)


# ---------------------------------------------------------------------
# Production safety checks
# ---------------------------------------------------------------------
# When DEBUG is off, missing or placeholder settings should fail fast.
if not DEBUG:
    if not SECRET_KEY or SECRET_KEY in {"change-me", "dev-change-me"}:
        raise RuntimeError("SECRET_KEY must be set to a strong value when DEBUG=0.")
    if not ALLOWED_HOSTS:
        raise RuntimeError("ALLOWED_HOSTS must be set when DEBUG=0.")


# ---------------------------------------------------------------------
# Application definition
# ---------------------------------------------------------------------
INSTALLED_APPS = [
    "core.apps.CoreConfig",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "crispy_forms",
    "crispy_bootstrap5",
    "accounts",
    "srp",
    "eve_sso",
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

ROOT_URLCONF = "alliancehub.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "alliancehub.wsgi.application"


# ---------------------------------------------------------------------
# Database (PostgreSQL; commonly provided via Docker)
# ---------------------------------------------------------------------
# POSTGRES_HOST defaults to "db" to match a typical docker-compose service name.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env_str("POSTGRES_DB"),
        "USER": env_str("POSTGRES_USER"),
        "PASSWORD": env_str("POSTGRES_PASSWORD"),
        "HOST": env_str("POSTGRES_HOST", "db"),
        "PORT": env_int("POSTGRES_PORT", 5432),
    }
}


# ---------------------------------------------------------------------
# Password validation
# ---------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------
# Internationalization / Time
# ---------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env_str("TIME_ZONE", "UTC") or "UTC"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise serves static files in a simple, production-friendly way.
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------
# Forms / Auth
# ---------------------------------------------------------------------
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

AUTH_USER_MODEL = "accounts.User"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/"


# ---------------------------------------------------------------------
# EVE Online SSO / ESI
# ---------------------------------------------------------------------
# These values are environment-specific because they change with domain/registration.
EVE_CLIENT_ID = env_str("EVE_CLIENT_ID")
EVE_CLIENT_SECRET = env_str("EVE_CLIENT_SECRET")
EVE_CALLBACK_URL = env_str("EVE_CALLBACK_URL")

EVE_AUTH_URL = env_str("EVE_AUTH_URL", "https://login.eveonline.com/v2/oauth/authorize")
EVE_TOKEN_URL = env_str("EVE_TOKEN_URL", "https://login.eveonline.com/v2/oauth/token")
EVE_VERIFY_URL = env_str("EVE_VERIFY_URL", "https://login.eveonline.com/oauth/verify")
EVE_ESI_URL = env_str("EVE_ESI_URL", "https://esi.evetech.net")

# Network timeout (seconds) for all EVE SSO + ESI HTTP calls.
# This prevents a stalled upstream request from tying up a web worker indefinitely.
EVE_HTTP_TIMEOUT = env_int("EVE_HTTP_TIMEOUT", 10)


# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
# Default logging goes to stdout (best for Docker). Optional file logging can be enabled.
LOG_LEVEL = env_str("LOG_LEVEL", "WARNING") or "WARNING"
LOG_TO_FILE = env_bool("LOG_TO_FILE", False)
LOG_FILE_PATH = env_str("LOG_FILE_PATH", "logs/app.log") or "logs/app.log"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
        "simple": {"format": "{levelname}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL},
        "eve_sso": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "srp": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

if LOG_TO_FILE:
    LOGGING["handlers"]["file"] = {
        "class": "logging.FileHandler",
        "filename": LOG_FILE_PATH,
        "formatter": "verbose",
    }
    for name in ["django", "eve_sso", "srp"]:
        LOGGING["loggers"][name]["handlers"].append("file")
