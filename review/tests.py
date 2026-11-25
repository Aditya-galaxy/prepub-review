"""Tests for the rules that matter: the embargo, the token lifecycle, and the audit trail.

Each test states the rule it is defending, because a test whose purpose is not obvious gets
deleted by the next person who sees it fail.
"""
import shutil
import tempfile
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from .audit import hash_token
from .models import AccessRecord, Deposition, Depositor, ReviewToken
from .orcid import InvalidORCID, is_valid, normalise

CIF = b"data_TEST\n_entry.id TEST\n"


class ORCIDTests(TestCase):
    def test_accepts_a_correct_check_digit(self):
        self.assertEqual(normalise("0000-0002-1694-233X"), "0000-0002-1694-233X")

    def test_accepts_a_uri_and_unhyphenated_form(self):
        self.assertEqual(normalise("https://orcid.org/0000000216942 33X".replace(" ", "")),
                         "0000-0002-1694-233X")

    def test_rejects_a_wrong_check_digit(self):
        """The point of validating at all: a transposed digit must not be stored."""
        with self.assertRaises(InvalidORCID):
            normalise("0000-0002-1694-2330")

    def test_rejects_wrong_length(self):
        self.assertFalse(is_valid("0000-0002-1694"))

    def test_model_validation_rejects_a_bad_id(self):
        depositor = Depositor(name="A Researcher", orcid="0000-0002-1694-2330")
        with self.assertRaises((ValidationError, InvalidORCID)):
            depositor.full_clean()

    def test_orcid_is_optional(self):
        """Real PDB audit authors often have no iD, and inventing one would be a fabrication."""
        depositor = Depositor.objects.create(name="No iD Recorded", orcid=None)
        self.assertIsNone(depositor.orcid)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="prepub-test-"))
class EmbargoBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        from django.conf import settings
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.depositor = Depositor.objects.create(name="A Researcher", orcid="0000-0002-1694-233X")
        today = timezone.localdate()
        self.embargoed = Deposition.objects.create(
            accession="9XYZ", title="Embargoed structure", depositor=self.depositor,
            method=Deposition.Method.EM, deposited_on=today - timedelta(days=10),
            release_on=today + timedelta(days=90), embargo_simulated=True)
        self.embargoed.coordinates.save("9XYZ.cif", ContentFile(CIF), save=True)
        self.released = Deposition.objects.create(
            accession="1ABC", title="Released structure", depositor=self.depositor,
            method=Deposition.Method.XRAY, deposited_on=today - timedelta(days=400),
            release_on=today - timedelta(days=30))

    def get(self, path, token=None):
        headers = {"HTTP_AUTHORIZATION": f"Review {token}"} if token else {}
        return self.client.get(path, **headers)


class EmbargoTests(EmbargoBase):
    def test_released_entry_is_public(self):
        response = self.get("/api/depositions/1ABC/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["embargoed"])

    def test_embargoed_entry_answers_404_without_a_token(self):
        """404 rather than 403: a 403 would confirm the entry exists, which is itself private."""
        self.assertEqual(self.get("/api/depositions/9XYZ/").status_code, 404)

    def test_embargoed_entry_opens_with_its_own_token(self):
        _, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        response = self.get("/api/depositions/9XYZ/", token=plaintext)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["embargoed"])

    def test_token_does_not_open_a_different_entry(self):
        """A token is scoped to one deposition. This is the whole authorisation rule."""
        other = Deposition.objects.create(
            accession="9ABD", title="Another embargoed structure", depositor=self.depositor,
            deposited_on=timezone.localdate(), release_on=timezone.localdate() + timedelta(days=60))
        _, plaintext = ReviewToken.issue(other, issued_for="J. Struct / MS-2")
        self.assertEqual(self.get("/api/depositions/9XYZ/", token=plaintext).status_code, 404)
        self.assertEqual(AccessRecord.objects.first().outcome,
                         AccessRecord.Outcome.DENIED_WRONG_ENTRY)

    def test_embargoed_entry_is_never_listed(self):
        accessions = [row["accession"] for row in self.get("/api/depositions/").json()]
        self.assertIn("1ABC", accessions)
        self.assertNotIn("9XYZ", accessions)

    def test_embargoed_entry_is_not_listed_even_with_a_valid_token(self):
        _, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        accessions = [row["accession"] for row in self.get("/api/depositions/", token=plaintext).json()]
        self.assertNotIn("9XYZ", accessions)

    def test_coordinates_refused_without_a_token(self):
        self.assertEqual(self.get("/api/depositions/9XYZ/coordinates/").status_code, 404)

    def test_coordinates_served_with_a_token(self):
        _, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        response = self.get("/api/depositions/9XYZ/coordinates/", token=plaintext)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), CIF)


class TokenLifecycleTests(EmbargoBase):
    def test_plaintext_token_is_never_stored(self):
        """A database copy must not hand the reader working access."""
        token, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        self.assertNotIn(plaintext, [token.token_hash, token.prefix])
        self.assertEqual(token.token_hash, hash_token(plaintext))
        self.assertFalse(ReviewToken.objects.filter(token_hash=plaintext).exists())

    def test_expired_token_is_rejected(self):
        token, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=["expires_at"])
        self.assertEqual(self.get("/api/depositions/9XYZ/", token=plaintext).status_code, 401)
        self.assertEqual(AccessRecord.objects.first().outcome, AccessRecord.Outcome.DENIED_EXPIRED)

    def test_revoked_token_is_rejected(self):
        token, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        token.revoke()
        self.assertEqual(self.get("/api/depositions/9XYZ/", token=plaintext).status_code, 401)
        self.assertEqual(AccessRecord.objects.first().outcome, AccessRecord.Outcome.DENIED_REVOKED)

    def test_access_limit_is_enforced(self):
        _, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1",
                                         max_accesses=1)
        self.assertEqual(self.get("/api/depositions/9XYZ/", token=plaintext).status_code, 200)
        self.assertEqual(self.get("/api/depositions/9XYZ/", token=plaintext).status_code, 401)
        self.assertEqual(AccessRecord.objects.first().outcome,
                         AccessRecord.Outcome.DENIED_EXHAUSTED)

    def test_unknown_token_is_rejected(self):
        self.assertEqual(self.get("/api/depositions/9XYZ/", token="not-a-real-token").status_code, 401)
        self.assertEqual(AccessRecord.objects.first().outcome, AccessRecord.Outcome.DENIED_UNKNOWN)

    def test_session_reports_what_the_token_opens(self):
        _, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-7",
                                         max_accesses=3)
        body = self.get("/api/session/", token=plaintext).json()
        self.assertEqual(body["accession"], "9XYZ")
        self.assertEqual(body["issued_for"], "J. Struct / MS-7")
        self.assertEqual(body["accesses_remaining"], 3)


class AuditTrailTests(EmbargoBase):
    def test_every_attempt_is_recorded_including_refusals(self):
        self.get("/api/depositions/9XYZ/")
        self.get("/api/depositions/1ABC/")
        self.get("/api/depositions/0000/")
        outcomes = set(AccessRecord.objects.values_list("outcome", flat=True))
        self.assertEqual(outcomes, {AccessRecord.Outcome.DENIED_NO_TOKEN,
                                    AccessRecord.Outcome.PUBLIC,
                                    AccessRecord.Outcome.NOT_FOUND})

    def test_record_keeps_the_token_prefix_but_not_the_token(self):
        _, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        self.get("/api/depositions/9XYZ/", token=plaintext)
        record = AccessRecord.objects.first()
        self.assertEqual(record.token_prefix, plaintext[:8])
        self.assertNotIn(plaintext, record.path)

    def test_client_address_is_hashed_not_stored(self):
        """An IP address is a personal identifier; a keyed digest answers the only question asked."""
        self.client.get("/api/depositions/1ABC/", REMOTE_ADDR="203.0.113.7")
        record = AccessRecord.objects.first()
        self.assertTrue(record.client_fingerprint)
        self.assertNotIn("203.0.113.7", record.client_fingerprint)

    def test_record_cannot_be_modified(self):
        self.get("/api/depositions/1ABC/")
        record = AccessRecord.objects.first()
        record.outcome = AccessRecord.Outcome.PUBLIC
        with self.assertRaises(ValueError):
            record.save()

    def test_record_cannot_be_deleted(self):
        self.get("/api/depositions/1ABC/")
        with self.assertRaises(ValueError):
            AccessRecord.objects.first().delete()

    def test_audit_survives_the_token_being_deleted(self):
        """An audit trail that loses rows with its foreign keys is not an audit trail."""
        token, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        self.get("/api/depositions/9XYZ/", token=plaintext)
        token.delete()
        record = AccessRecord.objects.first()
        self.assertEqual(record.accession, "9XYZ")
        self.assertEqual(record.token_prefix, plaintext[:8])


class AuditCompletenessTests(EmbargoBase):
    def test_unknown_token_record_still_names_the_targeted_entry(self):
        """Authentication rejects before any view runs, so the accession comes from the URL.

        Without this, the record for the most suspicious request, someone trying a token that does
        not exist, would not say what they were trying to reach.
        """
        self.get("/api/depositions/9XYZ/", token="not-a-real-token")
        record = AccessRecord.objects.first()
        self.assertEqual(record.outcome, AccessRecord.Outcome.DENIED_UNKNOWN)
        self.assertEqual(record.accession, "9XYZ")

    def test_expired_token_record_names_the_targeted_entry(self):
        token, plaintext = ReviewToken.issue(self.embargoed, issued_for="J. Struct / MS-1")
        token.expires_at = timezone.now() - timedelta(seconds=1)
        token.save(update_fields=["expires_at"])
        self.get("/api/depositions/9XYZ/", token=plaintext)
        self.assertEqual(AccessRecord.objects.first().accession, "9XYZ")


class AppendOnlyEnforcementTests(EmbargoBase):
    """Model-level save() and delete() are not enough on their own.

    Queryset update() and delete() compile straight to SQL and never call the model's methods, so
    without closing them too, one line could empty the audit trail.
    """

    def setUp(self):
        super().setUp()
        self.get("/api/depositions/1ABC/")
        self.assertEqual(AccessRecord.objects.count(), 1)

    def test_bulk_delete_is_refused(self):
        with self.assertRaises(ValueError):
            AccessRecord.objects.all().delete()
        self.assertEqual(AccessRecord.objects.count(), 1)

    def test_bulk_update_is_refused(self):
        with self.assertRaises(ValueError):
            AccessRecord.objects.all().update(status_code=500)
        self.assertEqual(AccessRecord.objects.first().status_code, 200)

    def test_records_can_still_be_created(self):
        """The guard must not break the only write path the service needs."""
        self.get("/api/depositions/1ABC/")
        self.assertEqual(AccessRecord.objects.count(), 2)


# A hand-written mmCIF fragment, not a real entry. It is deliberately fake so that no reader
# mistakes it for archive data, and it exercises the syntax that real files actually contain:
# a semicolon text field, an apostrophe inside a double-quoted value, a loop_ with placeholders,
# and comments.
SAMPLE_CIF = """data_9XYZ
#
_entry.id   9XYZ
#
_struct.title
;Embargoed structure
;
#
_exptl.method   'ELECTRON MICROSCOPY'      # not a refinement, so no _refine category
_em_3d_reconstruction.resolution   3.40
#
loop_
_audit_author.name
_audit_author.pdbx_ordinal
_audit_author.identifier_ORCID
"O'Brien, S."   1   0000-0002-1694-233X
'Jones, A.'     2   ?
.               3   .
#
"""


class MMCIFParsingTests(TestCase):
    def test_reads_a_plain_item(self):
        from .mmcif import header
        self.assertEqual(header(SAMPLE_CIF).entry_id, "9XYZ")

    def test_reads_a_semicolon_text_field(self):
        """The only way mmCIF carries a multi-line value, and common in titles."""
        from .mmcif import header
        self.assertEqual(header(SAMPLE_CIF).title, "Embargoed structure")

    def test_apostrophe_inside_a_double_quoted_value_does_not_truncate_it(self):
        """A naive parser turns "O'Brien, S." into O, which silently corrupts an author list."""
        from .mmcif import header
        self.assertEqual(header(SAMPLE_CIF).authors[0]["name"], "O'Brien, S.")

    def test_loop_placeholders_become_none_not_strings(self):
        """`?` means unknown and `.` means not applicable. Neither is data."""
        from .mmcif import header
        authors = header(SAMPLE_CIF).authors
        self.assertEqual(authors[0]["orcid"], "0000-0002-1694-233X")
        self.assertIsNone(authors[1]["orcid"])
        self.assertIsNone(authors[2]["name"])

    def test_em_resolution_is_read_from_the_right_category(self):
        """A cryo-EM entry has no _refine.ls_d_res_high; its resolution lives elsewhere."""
        from .mmcif import header
        self.assertEqual(header(SAMPLE_CIF).resolution, 3.40)

    def test_comments_are_ignored(self):
        from .mmcif import header
        self.assertEqual(header(SAMPLE_CIF).method, "ELECTRON MICROSCOPY")

    def test_unterminated_quote_is_an_error_not_a_silent_truncation(self):
        from .mmcif import MMCIFError, header
        with self.assertRaises(MMCIFError):
            header("data_X\n_struct.title   'never closed\n")

    def test_unterminated_semicolon_field_is_an_error(self):
        from .mmcif import MMCIFError, header
        with self.assertRaises(MMCIFError):
            header("data_X\n_struct.title\n;runs off the end\n")

    def test_a_file_that_is_not_mmcif_is_rejected(self):
        from .mmcif import MMCIFError, header
        with self.assertRaises(MMCIFError):
            header("just some text\n")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="prepub-cif-"))
class CoordinateVerificationTests(TestCase):
    """The curation question: does the uploaded file say what the record says?"""

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.depositor = Depositor.objects.create(name="O'Brien, S.",
                                                  orcid="0000-0002-1694-233X")
        today = timezone.localdate()
        self.deposition = Deposition.objects.create(
            accession="9XYZ", title="Embargoed structure", depositor=self.depositor,
            method=Deposition.Method.EM, resolution=3.40,
            deposited_on=today, release_on=today + timedelta(days=90))

    def attach(self, text=SAMPLE_CIF):
        self.deposition.coordinates.save("9XYZ.cif", ContentFile(text.encode()), save=True)

    def test_matching_file_reports_nothing(self):
        self.attach()
        self.assertEqual(self.deposition.verify_coordinates(), [])

    def test_wrong_accession_is_caught(self):
        """The commonest real mistake: the wrong file uploaded against a record."""
        self.attach(SAMPLE_CIF.replace("_entry.id   9XYZ", "_entry.id   1ABC"))
        fields = [row[0] for row in self.deposition.verify_coordinates()]
        self.assertIn("accession", fields)

    def test_changed_title_is_caught(self):
        self.attach(SAMPLE_CIF.replace("Embargoed structure", "A quite different structure"))
        fields = [row[0] for row in self.deposition.verify_coordinates()]
        self.assertIn("title", fields)

    def test_changed_method_is_caught(self):
        self.attach(SAMPLE_CIF.replace("'ELECTRON MICROSCOPY'", "'SOLUTION NMR'"))
        fields = [row[0] for row in self.deposition.verify_coordinates()]
        self.assertIn("method", fields)

    def test_resolution_tolerance_allows_rounding_but_not_a_real_change(self):
        self.attach(SAMPLE_CIF.replace("3.40", "3.42"))
        self.assertEqual(self.deposition.verify_coordinates(), [])
        self.deposition.coordinates.delete(save=True)
        self.attach(SAMPLE_CIF.replace("3.40", "2.10"))
        fields = [row[0] for row in self.deposition.verify_coordinates()]
        self.assertIn("resolution", fields)

    def test_depositor_orcid_missing_from_the_file_is_caught(self):
        self.attach(SAMPLE_CIF.replace("0000-0002-1694-233X", "0000-0003-1567-1643"))
        fields = [row[0] for row in self.deposition.verify_coordinates()]
        self.assertIn("depositor ORCID", fields)

    def test_whitespace_differences_are_not_discrepancies(self):
        """A title rewrapped across lines is the same title."""
        self.attach(SAMPLE_CIF.replace(";Embargoed structure\n;",
                                       ";Embargoed\nstructure\n;"))
        self.assertEqual(self.deposition.verify_coordinates(), [])

    def test_no_file_is_distinguished_from_a_file_that_agrees(self):
        with self.assertRaises(FileNotFoundError):
            self.deposition.verify_coordinates()
