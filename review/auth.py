"""Token authentication for reviewers.

Reviewers are not user accounts. They hold a bearer token that is valid for one deposition, for a
limited time, and the service deliberately knows nothing else about them. So authentication here
resolves a token rather than a person, and the sentinel below exists only to satisfy the parts of
DRF that expect request.user to be something.
"""
from rest_framework import authentication, exceptions

from . import audit
from .models import AccessRecord, ReviewToken

KEYWORD = "Review"


class Reviewer:
    """A holder of a valid review token. Deliberately carries no identity."""

    is_authenticated = True
    is_anonymous = False
    is_staff = False

    def __str__(self):
        return "reviewer"


class ReviewTokenAuthentication(authentication.BaseAuthentication):
    """Reads `Authorization: Review <token>`.

    Returning None means "no credentials offered", which leaves the view free to serve a released
    entry to the public. Raising means credentials were offered and were not good.
    """

    keyword = KEYWORD

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            audit.mark(request, outcome=AccessRecord.Outcome.DENIED_UNKNOWN)
            raise exceptions.AuthenticationFailed("Malformed Authorization header")

        try:
            plaintext = header[1].decode()
        except UnicodeError:
            audit.mark(request, outcome=AccessRecord.Outcome.DENIED_UNKNOWN)
            raise exceptions.AuthenticationFailed("Malformed token")

        audit.mark(request, token_prefix=audit.token_prefix(plaintext))
        token = ReviewToken.objects.filter(token_hash=audit.hash_token(plaintext)).first()
        if token is None:
            audit.mark(request, outcome=AccessRecord.Outcome.DENIED_UNKNOWN)
            raise exceptions.AuthenticationFailed("Token not recognised")

        refusal = token.refusal_reason()
        if refusal is not None:
            audit.mark(request, outcome=refusal)
            raise exceptions.AuthenticationFailed("Token is no longer valid")

        return Reviewer(), token

    def authenticate_header(self, request):
        return self.keyword
