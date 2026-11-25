"""Sign a depositor in with ORCID, using the OAuth2 authorisation-code flow and OIDC.

Two kinds of principal reach this service and they are authenticated differently, which mirrors
how a real deposition system works. A reviewer holds a bearer token issued by a journal and is
deliberately anonymous. A depositor is a named person who already has an ORCID iD, and signs in
with it to see their own work.

The flow here is the standard authorisation code flow:

  1. /auth/orcid/login/ sends the browser to ORCID with a `state` and a `nonce`, both random and
     both remembered in the session.
  2. ORCID sends the browser back to /auth/orcid/callback/ with a code.
  3. This server exchanges the code for a token over a direct back-channel call to ORCID.

The iD therefore arrives over a server-to-server TLS connection rather than through the browser,
so it cannot be tampered with in transit. When ORCID also returns an OpenID Connect `id_token`,
it is verified properly against ORCID's published signing keys and its `sub` is cross-checked
against the iD in the token response. If those two disagree, the sign-in is refused rather than
resolved in favour of either.

Defaults point at ORCID's sandbox. Running against the real registry has to be a deliberate act,
not something that happens because an environment variable was forgotten.
"""
import json
import secrets
import urllib.parse
import urllib.request

import jwt
from django.conf import settings

from .orcid import InvalidORCID, normalise

STATE_KEY = "orcid_oauth_state"
NONCE_KEY = "orcid_oauth_nonce"
SESSION_ORCID = "orcid_id"
SCOPE = "/authenticate openid"


class ORCIDAuthError(Exception):
    """Anything that means we cannot trust the identity. Never shown verbatim to the browser."""


def _base():
    return settings.ORCID_BASE.rstrip("/")


def authorize_url(session):
    """Build the ORCID URL and remember the one-time values that prove the reply is ours."""
    if not settings.ORCID_CLIENT_ID:
        raise ORCIDAuthError("ORCID_CLIENT_ID is not configured")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    session[STATE_KEY] = state
    session[NONCE_KEY] = nonce
    query = urllib.parse.urlencode({
        "client_id": settings.ORCID_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": settings.ORCID_REDIRECT_URI,
        "state": state,
        "nonce": nonce,
    })
    return f"{_base()}/oauth/authorize?{query}"


def exchange_code(code):
    """Trade the code for a token. Isolated so tests can replace the one network call."""
    payload = urllib.parse.urlencode({
        "client_id": settings.ORCID_CLIENT_ID,
        "client_secret": settings.ORCID_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.ORCID_REDIRECT_URI,
    }).encode()
    request = urllib.request.Request(
        f"{_base()}/oauth/token", data=payload,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except Exception as exc:                                       # noqa: BLE001
        raise ORCIDAuthError(f"token exchange failed: {type(exc).__name__}") from exc


def verify_id_token(id_token, nonce):
    """Check an OIDC id_token against ORCID's published keys, and return its claims.

    Signature, issuer, audience and expiry are all checked by the library. The nonce is checked
    here, because it is what ties this token to the login this browser actually started.
    """
    try:
        signing_key = jwt.PyJWKClient(f"{_base()}/oauth/jwks").get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token, signing_key.key, algorithms=["RS256"],
            audience=settings.ORCID_CLIENT_ID, issuer=_base(),
            options={"require": ["exp", "iat", "sub", "aud", "iss"]})
    except Exception as exc:                                       # noqa: BLE001
        raise ORCIDAuthError(f"id_token rejected: {type(exc).__name__}") from exc

    if claims.get("nonce") != nonce:
        raise ORCIDAuthError("id_token nonce does not match this login")
    return claims


def complete(session, code, state):
    """Finish the flow and return the verified ORCID iD.

    The state is removed from the session before anything else, so a replayed callback cannot be
    used a second time.
    """
    expected_state = session.pop(STATE_KEY, None)
    nonce = session.pop(NONCE_KEY, None)
    if not expected_state or not state or not secrets.compare_digest(state, expected_state):
        raise ORCIDAuthError("state did not match; the callback was not for this session")

    payload = exchange_code(code)
    raw = payload.get("orcid")
    if not raw:
        raise ORCIDAuthError("token response carried no ORCID iD")

    try:
        orcid = normalise(raw)
    except InvalidORCID as exc:
        # ORCID is authoritative, so this should not happen; failing loudly beats storing an iD
        # that will not match anything.
        raise ORCIDAuthError(f"ORCID returned an iD that does not validate: {exc}") from exc

    id_token = payload.get("id_token")
    if id_token:
        claims = verify_id_token(id_token, nonce)
        try:
            from_token = normalise(claims["sub"])
        except (KeyError, InvalidORCID) as exc:
            raise ORCIDAuthError("id_token subject is not a usable ORCID iD") from exc
        if from_token != orcid:
            raise ORCIDAuthError("id_token subject disagrees with the token response")

    return orcid
