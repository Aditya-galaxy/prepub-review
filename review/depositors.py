"""Who is signed in, if anyone.

Depositors are not Django users. They are rows in the Depositor table identified by an ORCID iD
that ORCID itself vouched for, and the session holds nothing but that iD. Resolving it to a row on
every request means that deleting a depositor takes effect immediately rather than when their
session happens to expire.
"""
from .models import Depositor
from .orcid_oauth import SESSION_ORCID


def signed_in_depositor(request):
    """The Depositor for the signed-in ORCID iD, or None."""
    session = getattr(getattr(request, "_request", request), "session", None)
    if session is None:
        return None
    orcid = session.get(SESSION_ORCID)
    if not orcid:
        return None
    return Depositor.objects.filter(orcid=orcid).first()
