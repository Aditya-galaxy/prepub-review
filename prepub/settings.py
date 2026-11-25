"""Settings for the pre-publication review service.

Two choices here are deliberate and are explained in the README: secrets come from the
environment with development-only fallbacks, and the audit middleware is listed last so that it
wraps the view and still records a request that authentication rejected.
"""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key-do-not-deploy")
DEBUG = _env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

# Separate from SECRET_KEY so that rotating a web session key does not silently invalidate every
# stored token hash and break the audit trail's ability to link records to tokens.
TOKEN_PEPPER = os.environ.get("PREPUB_TOKEN_PEPPER", "dev-only-token-pepper")
# Client addresses are hashed with this before storage. See review/audit.py.
CLIENT_SALT = os.environ.get("PREPUB_CLIENT_SALT", "dev-only-client-salt")

# ORCID sign-in for depositors. The default is the SANDBOX: pointing this at the real registry
# has to be a deliberate act, not something that happens because a variable was forgotten.
# A cryo-EM entry can run to tens of megabytes, so the ceiling is generous, but it exists:
# an upload endpoint with no limit is a way to fill the disk.
MAX_UPLOAD_BYTES = int(os.environ.get("PREPUB_MAX_UPLOAD_BYTES", 50 * 1024 * 1024))

ORCID_BASE = os.environ.get("ORCID_BASE", "https://sandbox.orcid.org")
ORCID_CLIENT_ID = os.environ.get("ORCID_CLIENT_ID", "")
ORCID_CLIENT_SECRET = os.environ.get("ORCID_CLIENT_SECRET", "")
ORCID_REDIRECT_URI = os.environ.get(
    "ORCID_REDIRECT_URI", "http://127.0.0.1:8000/auth/orcid/callback/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "review",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "review.middleware.AccessAuditMiddleware",
]

ROOT_URLCONF = "prepub.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "prepub.wsgi.application"


def _database_from_env():
    """PostgreSQL in deployment, SQLite for a checkout with nothing installed.

    Parsed here rather than with dj-database-url to keep the dependency list to Django, DRF and
    gunicorn, so that `pip install -r requirements.txt` cannot fail on an unrelated package.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    bits = urlparse(url)
    if bits.scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"unsupported DATABASE_URL scheme: {bits.scheme}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": bits.path.lstrip("/"),
        "USER": unquote(bits.username or ""),
        "PASSWORD": unquote(bits.password or ""),
        "HOST": bits.hostname or "",
        "PORT": str(bits.port or ""),
    }


DATABASES = {"default": _database_from_env()}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["review.auth.ReviewTokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Deposited coordinate files live here. There is deliberately no MEDIA_URL and no static
# route to this directory: an embargoed file must only ever leave through a view that has
# checked a token, so exposing it to the web server would defeat the whole service.
MEDIA_ROOT = Path(os.environ.get("PREPUB_MEDIA_ROOT", BASE_DIR / "media"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
    # A review token travels in a header. If any request is ever answered over plain HTTP the
    # token is exposed, so redirect and then tell the browser not to try HTTP again.
    SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Set when TLS terminates at a proxy, so Django can tell an HTTPS request from an HTTP one.
    if _env_bool("DJANGO_TRUST_PROXY_PROTO", False):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
