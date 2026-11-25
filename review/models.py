"""The data model for pre-publication access.

The domain rule this service exists to enforce: a deposition is confidential until its release
date. Before that date it is visible only to someone holding a valid review token for that exact
entry, and every attempt to reach it, successful or not, leaves a record.
"""
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from . import audit
from .orcid import InvalidORCID, normalise


def validate_orcid(value):
    try:
        normalise(value)
    except InvalidORCID as exc:
        raise ValidationError(str(exc)) from exc


class Depositor(models.Model):
    name = models.CharField(max_length=200)
    orcid = models.CharField(max_length=19, unique=True, null=True, blank=True,
                             validators=[validate_orcid], help_text="0000-0000-0000-0000")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def clean(self):
        # Normalise before the unique check so the same iD written two ways cannot be stored twice.
        if self.orcid:
            self.orcid = normalise(self.orcid)

    def save(self, *args, **kwargs):
        self.orcid = normalise(self.orcid) if self.orcid else None
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.orcid})"


class Deposition(models.Model):
    """One deposited structure. `release_on` is the date the embargo lifts."""

    class Method(models.TextChoices):
        XRAY = "X-RAY DIFFRACTION", "X-ray diffraction"
        EM = "ELECTRON MICROSCOPY", "Electron microscopy"
        NMR = "SOLUTION NMR", "Solution NMR"
        OTHER = "OTHER", "Other"

    accession = models.CharField(max_length=12, unique=True, db_index=True)
    title = models.CharField(max_length=400)
    method = models.CharField(max_length=32, choices=Method.choices, default=Method.OTHER)
    resolution = models.FloatField(null=True, blank=True, help_text="Angstrom, if applicable")
    depositor = models.ForeignKey(Depositor, on_delete=models.PROTECT, related_name="depositions")
    coordinates = models.FileField(upload_to="depositions/", null=True, blank=True,
                                   help_text="Never served by the web server directly; see views.py")
    deposited_on = models.DateField()
    release_on = models.DateField(help_text="The embargo lifts at the start of this date, UTC.")
    embargo_simulated = models.BooleanField(
        default=False,
        help_text="True when release_on was moved into the future to demonstrate the embargo. "
                  "Entries genuinely under embargo are not public, so their dates cannot be real.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["accession"]

    def save(self, *args, **kwargs):
        self.accession = self.accession.strip().upper()
        return super().save(*args, **kwargs)

    def clean(self):
        if self.release_on and self.deposited_on and self.release_on < self.deposited_on:
            raise ValidationError({"release_on": "a release date cannot precede the deposition date"})

    def is_embargoed(self, on=None):
        return (on or timezone.localdate()) < self.release_on

    def verify_coordinates(self):
        """Check the uploaded file's header against this record.

        Returns a list of (field, in_record, in_file). Empty means they agree. Raises
        FileNotFoundError when there is no file to check, because "no file" and "a file that
        agrees" are different answers and a caller should not confuse them.
        """
        from . import mmcif
        if not self.coordinates:
            raise FileNotFoundError(f"{self.accession} has no coordinate file")
        with self.coordinates.open("rb") as fh:
            text = fh.read().decode("utf-8", "replace")
        return mmcif.discrepancies(self, text)

    def __str__(self):
        return f"{self.accession}: {self.title[:60]}"


class ReviewTokenQuerySet(models.QuerySet):
    def live(self, at=None):
        at = at or timezone.now()
        return self.filter(revoked_at__isnull=True, expires_at__gt=at)


class ReviewToken(models.Model):
    """Time-limited access to one deposition, for one manuscript under review.

    The token itself is never stored. What is stored is an HMAC of it plus a short prefix, so that
    an audit record can be tied to a token without the table containing anything that grants access.

    `issued_for` names the journal and manuscript, not the reviewer. The service has no need for
    the reviewer's identity, and peer review is normally confidential, so it is not collected.
    """

    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prefix = models.CharField(max_length=audit.PREFIX_LENGTH)
    deposition = models.ForeignKey(Deposition, on_delete=models.CASCADE, related_name="tokens")
    issued_for = models.CharField(max_length=200, help_text="Journal and manuscript reference")
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                  null=True, blank=True, related_name="issued_tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    max_accesses = models.PositiveIntegerField(default=0, help_text="0 means no limit")
    access_count = models.PositiveIntegerField(default=0)

    objects = ReviewTokenQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, deposition, issued_for, valid_for_days=30, max_accesses=0, issued_by=None):
        """Create a token and return (instance, plaintext). The plaintext is shown once."""
        plaintext = audit.new_token()
        token = cls.objects.create(
            token_hash=audit.hash_token(plaintext),
            prefix=audit.token_prefix(plaintext),
            deposition=deposition,
            issued_for=issued_for,
            issued_by=issued_by,
            expires_at=timezone.now() + timedelta(days=valid_for_days),
            max_accesses=max_accesses,
        )
        return token, plaintext

    def refusal_reason(self, at=None):
        """None if the token may be used, otherwise an AccessRecord.Outcome value."""
        at = at or timezone.now()
        if self.revoked_at is not None:
            return AccessRecord.Outcome.DENIED_REVOKED
        if self.expires_at <= at:
            return AccessRecord.Outcome.DENIED_EXPIRED
        if self.max_accesses and self.access_count >= self.max_accesses:
            return AccessRecord.Outcome.DENIED_EXHAUSTED
        return None

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])

    def record_use(self):
        # F() so that concurrent reviewers cannot both read the same count and overshoot a limit.
        ReviewToken.objects.filter(pk=self.pk).update(access_count=models.F("access_count") + 1)

    def __str__(self):
        return f"{self.prefix}... for {self.deposition.accession}"


class AppendOnlyQuerySet(models.QuerySet):
    """Close the bulk routes.

    Overriding save() and delete() on the model is not enough: queryset update() and delete()
    go straight to SQL and never call them. Without this, `AccessRecord.objects.all().delete()`
    would quietly empty the audit trail, and "append-only" would be a comment rather than a
    guarantee.
    """

    def update(self, **kwargs):
        raise ValueError("AccessRecord is append-only and cannot be updated")

    def delete(self):
        raise ValueError("AccessRecord is append-only and cannot be deleted")


class AccessRecord(models.Model):
    """One attempt to reach a deposition. Append-only.

    `accession` and `token_prefix` are copied in as text rather than left as foreign keys. An audit
    trail that loses its rows when a token is deleted is not an audit trail, and the question it
    has to answer later is "who reached this entry, and when", which must survive the entry and the
    token being removed.
    """

    class Outcome(models.TextChoices):
        PUBLIC = "public", "Public entry, no token required"
        GRANTED = "granted", "Granted on a valid token"
        GRANTED_DEPOSITOR = "granted_depositor", "Granted to the signed-in depositor"
        DEPOSITED = "deposited", "New deposition accepted"
        RELEASED = "released", "Embargo lifted by the depositor"
        DENIED_NO_TOKEN = "denied_no_token", "Embargoed, no token presented"
        DENIED_UNKNOWN = "denied_unknown_token", "Token not recognised"
        DENIED_EXPIRED = "denied_expired", "Token expired"
        DENIED_REVOKED = "denied_revoked", "Token revoked"
        DENIED_EXHAUSTED = "denied_exhausted", "Token access limit reached"
        DENIED_WRONG_ENTRY = "denied_wrong_entry", "Token valid for a different entry"
        DENIED_NOT_DEPOSITOR = "denied_not_depositor", "Signed in, but not this depositor"
        DENIED_NOT_AUTHOR = "denied_not_author", "Not an author of the file they uploaded"
        DENIED_BAD_UPLOAD = "denied_bad_upload", "Upload rejected"
        NOT_FOUND = "not_found", "No such entry"

    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=300)
    accession = models.CharField(max_length=12, blank=True, db_index=True)
    token_prefix = models.CharField(max_length=audit.PREFIX_LENGTH, blank=True)
    outcome = models.CharField(max_length=32, choices=Outcome.choices, db_index=True)
    status_code = models.PositiveSmallIntegerField()
    client_fingerprint = models.CharField(max_length=32, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ["-occurred_at"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("AccessRecord is append-only and cannot be modified")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("AccessRecord is append-only and cannot be deleted")

    def __str__(self):
        return f"{self.occurred_at:%Y-%m-%d %H:%M:%S} {self.outcome} {self.accession}"
