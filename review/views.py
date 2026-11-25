"""Read-only API for depositions, with the embargo enforced in one place.

One decision is worth stating plainly. An embargoed entry reached without a valid token answers
404, not 403. A 403 would confirm that the accession exists and is under embargo, which is itself
information about unpublished work: it tells a competitor that a given group has deposited a given
structure. The audit trail still records the real reason.

Tokens are issued through the Django admin and the `issue_token` management command, not through
this API. Keeping the issuing path off the public surface means a flaw in the reviewer-facing code
cannot be turned into a way to mint access.
"""
import logging

from django.http import FileResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_GET
from rest_framework import exceptions, generics, views
from rest_framework.response import Response

from . import audit, orcid_oauth
from .depositors import signed_in_depositor
from .models import AccessRecord, Deposition
from .serializers import DepositionSerializer, SessionSerializer

log = logging.getLogger(__name__)


def authorise(request, accession):
    """Return the deposition the caller may see, or raise NotFound.

    Expired, revoked and unrecognised tokens never arrive here: authentication rejects them first.
    """
    accession = accession.strip().upper()
    audit.mark(request, accession=accession)
    deposition = Deposition.objects.filter(accession=accession).select_related("depositor").first()

    if deposition is None:
        audit.mark(request, outcome=AccessRecord.Outcome.NOT_FOUND)
        raise exceptions.NotFound("No such entry")

    if not deposition.is_embargoed():
        audit.mark(request, outcome=AccessRecord.Outcome.PUBLIC)
        return deposition

    token = getattr(request, "auth", None)
    if token is not None:
        if token.deposition_id != deposition.pk:
            audit.mark(request, outcome=AccessRecord.Outcome.DENIED_WRONG_ENTRY)
            raise exceptions.NotFound("No such entry")
        audit.mark(request, outcome=AccessRecord.Outcome.GRANTED)
        token.record_use()
        return deposition

    # A depositor signed in with ORCID may read their own embargoed work. This is a third
    # route to confidential data, so it is recorded as its own outcome rather than filed
    # under a token grant, and someone else's entry is still a 404 rather than a 403.
    depositor = signed_in_depositor(request)
    if depositor is not None:
        if deposition.depositor_id == depositor.pk:
            audit.mark(request, outcome=AccessRecord.Outcome.GRANTED_DEPOSITOR)
            return deposition
        audit.mark(request, outcome=AccessRecord.Outcome.DENIED_NOT_DEPOSITOR)
        raise exceptions.NotFound("No such entry")

    audit.mark(request, outcome=AccessRecord.Outcome.DENIED_NO_TOKEN)
    raise exceptions.NotFound("No such entry")


class DepositionList(generics.ListAPIView):
    """Released entries only. An embargoed entry is never listed, with or without a token."""

    serializer_class = DepositionSerializer

    def get_queryset(self):
        from django.utils import timezone
        return (Deposition.objects
                .filter(release_on__lte=timezone.localdate())
                .select_related("depositor"))


class DepositionDetail(views.APIView):
    def get(self, request, accession):
        deposition = authorise(request, accession)
        return Response(DepositionSerializer(deposition).data)


class DepositionCoordinates(views.APIView):
    """Serve the coordinate file through the application, after the same check.

    This is why MEDIA_ROOT has no URL route: the file must not be reachable any other way.
    """

    def get(self, request, accession):
        deposition = authorise(request, accession)
        if not deposition.coordinates:
            raise exceptions.NotFound("No coordinate file for this entry")
        return FileResponse(deposition.coordinates.open("rb"),
                            as_attachment=True,
                            filename=f"{deposition.accession}.cif")


class SessionView(views.APIView):
    """What the presented token can reach. Requires a token."""

    def get(self, request):
        token = getattr(request, "auth", None)
        if token is None:
            audit.mark(request, outcome=AccessRecord.Outcome.DENIED_NO_TOKEN)
            raise exceptions.NotAuthenticated("A review token is required")
        audit.mark(request, accession=token.deposition.accession,
                   outcome=AccessRecord.Outcome.GRANTED)
        remaining = None
        if token.max_accesses:
            remaining = max(token.max_accesses - token.access_count, 0)
        return Response(SessionSerializer({
            "accession": token.deposition.accession,
            "issued_for": token.issued_for,
            "expires_at": token.expires_at,
            "accesses_remaining": remaining,
        }).data)


class MyDepositions(generics.ListAPIView):
    """The signed-in depositor's own entries, embargoed ones included.

    This is the point of signing in: the public list deliberately hides embargoed entries, and a
    review token opens exactly one. A depositor should see their own work and nothing else.
    """

    serializer_class = DepositionSerializer

    def get_queryset(self):
        depositor = signed_in_depositor(self.request)
        if depositor is None:
            audit.mark(self.request, outcome=AccessRecord.Outcome.DENIED_NO_TOKEN)
            raise exceptions.NotAuthenticated("Sign in with ORCID first")
        audit.mark(self.request, outcome=AccessRecord.Outcome.GRANTED_DEPOSITOR)
        return Deposition.objects.filter(depositor=depositor).select_related("depositor")


@require_GET
def orcid_login(request):
    try:
        return redirect(orcid_oauth.authorize_url(request.session))
    except orcid_oauth.ORCIDAuthError as exc:
        log.warning("ORCID login could not start: %s", exc)
        return redirect("/?signin=unavailable")


@require_GET
def orcid_callback(request):
    """Finish the flow.

    Every failure redirects to the same place with the same message. The reasons are logged for
    the operator but not shown, because telling a caller which step failed helps someone probing
    the flow more than it helps a depositor who simply needs to try again.
    """
    if request.GET.get("error"):
        log.info("ORCID returned an error: %s", request.GET.get("error"))
        return redirect("/?signin=failed")

    try:
        orcid = orcid_oauth.complete(request.session,
                                     request.GET.get("code", ""),
                                     request.GET.get("state", ""))
    except orcid_oauth.ORCIDAuthError as exc:
        log.warning("ORCID sign-in refused: %s", exc)
        return redirect("/?signin=failed")

    from .models import Depositor
    if not Depositor.objects.filter(orcid=orcid).exists():
        # No account is created here. A person becomes a depositor by depositing, not by signing
        # in, and auto-creating rows would let anyone with an ORCID iD populate the table.
        log.info("ORCID sign-in by an iD with no depositions")
        return redirect("/?signin=unknown")

    # Cycle the session key on privilege change, so a session fixed before sign-in is not the one
    # that ends up carrying the identity.
    request.session.cycle_key()
    request.session[orcid_oauth.SESSION_ORCID] = orcid
    return redirect("/?signin=ok")


@require_GET
def orcid_logout(request):
    request.session.flush()
    return redirect("/?signin=out")
