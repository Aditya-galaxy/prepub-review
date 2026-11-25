"""Hashing helpers shared by the token store and the audit trail.

Two things are hashed before they reach the database, for different reasons.

Review tokens are hashed so that a database copy does not hand the reader working access to
embargoed science. HMAC rather than a bare digest, so that an attacker who obtains the table still
cannot test candidate tokens offline without the pepper.

Client addresses are hashed so that the audit trail can answer "was this the same client?" without
storing a personal identifier. An IP address identifies a person under the GDPR; a keyed digest of
one supports the only question the audit needs to answer.
"""
import hashlib
import hmac
import secrets

from django.conf import settings

TOKEN_BYTES = 32
PREFIX_LENGTH = 8


def new_token():
    """A fresh URL-safe token. Returned once, never stored in this form."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token):
    return hmac.new(settings.TOKEN_PEPPER.encode(), str(token).encode(), hashlib.sha256).hexdigest()


def token_prefix(token):
    """The leading characters, kept in clear so a log line can be tied to a token.

    Short enough that it is not a usable secret, long enough to be unambiguous in practice.
    """
    return str(token)[:PREFIX_LENGTH]


def fingerprint_client(request):
    """A keyed, non-reversible digest of the client address."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    address = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    if not address:
        return ""
    return hmac.new(settings.CLIENT_SALT.encode(), address.encode(), hashlib.sha256).hexdigest()[:32]


def mark(request, **fields):
    """Record what really happened, for the middleware to pick up after the view returns.

    The client is often told less than the audit records: an embargoed entry answers 404 whether
    or not it exists, while the trail keeps the true reason. These are written onto the underlying
    HttpRequest because that, not the DRF wrapper, is what the middleware sees.
    """
    target = getattr(request, "_request", request)
    for name, value in fields.items():
        if value is not None:
            setattr(target, f"audit_{name}", value)
