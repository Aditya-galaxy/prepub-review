"""Read-only API for depositions, with the embargo enforced in one place.

One decision is worth stating plainly. An embargoed entry reached without a valid token answers
404, not 403. A 403 would confirm that the accession exists and is under embargo, which is itself
information about unpublished work: it tells a competitor that a given group has deposited a given
structure. The audit trail still records the real reason.

Tokens are issued through the Django admin and the `issue_token` management command, not through
this API. Keeping the issuing path off the public surface means a flaw in the reviewer-facing code
cannot be turned into a way to mint access.
"""
from django.http import FileResponse
from rest_framework import exceptions, generics, views
from rest_framework.response import Response

from . import audit
from .models import AccessRecord, Deposition
from .serializers import DepositionSerializer, SessionSerializer


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
    if token is None:
        audit.mark(request, outcome=AccessRecord.Outcome.DENIED_NO_TOKEN)
        raise exceptions.NotFound("No such entry")
    if token.deposition_id != deposition.pk:
        audit.mark(request, outcome=AccessRecord.Outcome.DENIED_WRONG_ENTRY)
        raise exceptions.NotFound("No such entry")

    audit.mark(request, outcome=AccessRecord.Outcome.GRANTED)
    token.record_use()
    return deposition


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
