"""Accept a new deposition from its own author.

The rule that makes this safe is narrow and worth stating: the record is built from the file,
never typed in alongside it, and the person uploading has to be named in the file as an author.
Together those mean a depositor cannot claim someone else's structure, and the record cannot
disagree with the file at the moment it is created.
"""
from django.conf import settings
from django.utils import timezone

from . import mmcif
from .models import Deposition


class DepositRejected(ValueError):
    """Why an upload was refused. Safe to show the uploader: it is about their own file."""


def _method_from(found):
    for choice in Deposition.Method:
        if choice.value == (found.method or ""):
            return choice.value
    return Deposition.Method.OTHER


def read_upload(upload):
    """Read and parse an uploaded file, or say why it cannot be used."""
    if upload.size > settings.MAX_UPLOAD_BYTES:
        raise DepositRejected(
            f"file is {upload.size} bytes, over the {settings.MAX_UPLOAD_BYTES} byte limit")
    payload = upload.read()
    upload.seek(0)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DepositRejected("file is not UTF-8 text; mmCIF is a text format") from exc
    try:
        found = mmcif.header(text)
    except mmcif.MMCIFError as exc:
        raise DepositRejected(f"could not read the file as mmCIF: {exc}") from exc
    if not found.entry_id:
        raise DepositRejected("the file has no _entry.id, so there is nothing to file it under")
    return found


def check_author(found, depositor):
    """The uploader must be named in the file.

    If the file lists no ORCID iD at all we refuse rather than wave it through. Authorship cannot
    be established from a name alone, and a deposition system that accepts "trust me" at this
    point has no access control worth the name. Real archives have other evidence available to
    them; this service does not.
    """
    if not depositor.orcid:
        raise DepositRejected("your account has no ORCID iD recorded, so authorship cannot be checked")
    in_file = {a["orcid"] for a in found.authors if a["orcid"]}
    if not in_file:
        raise DepositRejected(
            "the file records no ORCID iD for any author, so authorship cannot be checked")
    if depositor.orcid not in in_file:
        raise DepositRejected(
            "your ORCID iD is not among the authors recorded in this file")


def create(upload, depositor, release_on):
    """Build a Deposition from the file, after checking the uploader may do so."""
    found = read_upload(upload)
    check_author(found, depositor)

    accession = found.entry_id.strip().upper()
    if Deposition.objects.filter(accession=accession).exists():
        # Replacing a deposition is versioning, which this service does not do. Silently
        # overwriting would lose the file a reviewer may already have been reading.
        raise DepositRejected(f"{accession} already exists here")

    today = timezone.localdate()
    if release_on <= today:
        raise DepositRejected("the release date must be in the future, or there is no embargo")

    deposition = Deposition.objects.create(
        accession=accession,
        title=(found.title or accession)[:400],
        method=_method_from(found),
        resolution=found.resolution,
        depositor=depositor,
        deposited_on=today,
        release_on=release_on,
        embargo_simulated=False,
    )
    deposition.coordinates.save(f"{accession}.cif", upload, save=True)
    return deposition
