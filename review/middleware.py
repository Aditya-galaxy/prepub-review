"""Write one audit record per API request, in middleware rather than in each view.

A view that forgets to log is a hole in the trail, and the request that most needs recording is
the one that was rejected before any view ran. Putting this in middleware means the record is
written on every path through the application, including authentication failures and 404s.
"""
from .audit import fingerprint_client
from .models import AccessRecord

AUDITED_PREFIX = "/api/"


class AccessAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(AUDITED_PREFIX):
            try:
                self._record(request, response)
            except Exception:                                      # noqa: BLE001
                # An audit failure must not take the service down, but it must be visible.
                import logging
                logging.getLogger(__name__).exception("failed to write access record")
        return response

    @staticmethod
    def _accession(request):
        """Prefer what the view recorded, and fall back to the resolved URL.

        Authentication rejects a bad token before any view runs, so without this fallback the
        record for the most suspicious kind of request would not say which entry was targeted.
        """
        marked = getattr(request, "audit_accession", "")
        if marked:
            return marked
        match = getattr(request, "resolver_match", None)
        if match and match.kwargs.get("accession"):
            return str(match.kwargs["accession"]).strip().upper()
        return ""

    @classmethod
    def _record(cls, request, response):
        outcome = getattr(request, "audit_outcome", None)
        if outcome is None:
            outcome = (AccessRecord.Outcome.PUBLIC if response.status_code < 400
                       else AccessRecord.Outcome.NOT_FOUND)
        AccessRecord.objects.create(
            method=request.method[:8],
            path=request.path[:300],
            accession=cls._accession(request)[:12],
            token_prefix=getattr(request, "audit_token_prefix", "")[:8],
            outcome=outcome,
            status_code=response.status_code,
            client_fingerprint=fingerprint_client(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        )
