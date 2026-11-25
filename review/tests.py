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


# --- ORCID sign-in ----------------------------------------------------------------------------

ORCID_SETTINGS = dict(
    ORCID_BASE="https://sandbox.orcid.org",
    ORCID_CLIENT_ID="APP-TEST0000000000",
    ORCID_CLIENT_SECRET="test-secret",
    ORCID_REDIRECT_URI="http://testserver/auth/orcid/callback/",
)

DEPOSITOR_ORCID = "0000-0002-1694-233X"
OTHER_ORCID = "0000-0003-1567-1643"


def _rsa_keypair():
    """A throwaway key, so the id_token tests verify a real RS256 signature."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    return private, pem


@override_settings(**ORCID_SETTINGS)
class ORCIDFlowTests(TestCase):
    """The flow itself, with the single network call replaced."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key, cls.private_pem = _rsa_keypair()

    def setUp(self):
        from django.contrib.sessions.backends.db import SessionStore
        self.session = SessionStore()

    def start(self):
        from .orcid_oauth import authorize_url
        return authorize_url(self.session)

    def id_token(self, sub=DEPOSITOR_ORCID, nonce=None, audience=None):
        import time
        import jwt as pyjwt
        claims = {
            "iss": "https://sandbox.orcid.org",
            "sub": sub,
            "aud": audience or ORCID_SETTINGS["ORCID_CLIENT_ID"],
            "exp": int(time.time()) + 600,
            "iat": int(time.time()),
            "nonce": nonce,
        }
        return pyjwt.encode(claims, self.private_pem, algorithm="RS256")

    def complete(self, payload, state=None, patch_keys=True):
        from unittest import mock

        from . import orcid_oauth
        state = state if state is not None else self.session.get(orcid_oauth.STATE_KEY)
        key_holder = mock.Mock(key=self.private_key.public_key())
        with mock.patch.object(orcid_oauth, "exchange_code", return_value=payload), \
             mock.patch("jwt.PyJWKClient") as jwks:
            jwks.return_value.get_signing_key_from_jwt.return_value = key_holder
            return orcid_oauth.complete(self.session, "any-code", state)

    def test_authorize_url_carries_the_right_parameters(self):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.start()).query)
        self.assertEqual(query["client_id"], [ORCID_SETTINGS["ORCID_CLIENT_ID"]])
        self.assertEqual(query["response_type"], ["code"])
        self.assertIn("openid", query["scope"][0])
        self.assertTrue(query["state"][0])
        self.assertTrue(query["nonce"][0])

    def test_sandbox_is_the_default_target(self):
        """Reaching the real registry must be a deliberate act."""
        self.assertTrue(self.start().startswith("https://sandbox.orcid.org/oauth/authorize"))

    def test_login_without_a_client_id_is_refused(self):
        from .orcid_oauth import ORCIDAuthError, authorize_url
        with override_settings(ORCID_CLIENT_ID=""):
            with self.assertRaises(ORCIDAuthError):
                authorize_url(self.session)

    def test_a_valid_exchange_returns_the_id(self):
        self.start()
        from . import orcid_oauth
        nonce = self.session[orcid_oauth.NONCE_KEY]
        orcid = self.complete({"orcid": DEPOSITOR_ORCID, "id_token": self.id_token(nonce=nonce)})
        self.assertEqual(orcid, DEPOSITOR_ORCID)

    def test_mismatched_state_is_refused(self):
        """Without this check, any site could trigger a sign-in in a visitor's browser."""
        from .orcid_oauth import ORCIDAuthError
        self.start()
        with self.assertRaises(ORCIDAuthError):
            self.complete({"orcid": DEPOSITOR_ORCID}, state="not-the-state")

    def test_a_replayed_callback_is_refused(self):
        """State is single use: it is removed from the session on first use."""
        from .orcid_oauth import ORCIDAuthError
        self.start()
        from . import orcid_oauth
        state = self.session[orcid_oauth.STATE_KEY]
        self.complete({"orcid": DEPOSITOR_ORCID}, state=state)
        with self.assertRaises(ORCIDAuthError):
            self.complete({"orcid": DEPOSITOR_ORCID}, state=state)

    def test_a_response_with_no_id_is_refused(self):
        from .orcid_oauth import ORCIDAuthError
        self.start()
        with self.assertRaises(ORCIDAuthError):
            self.complete({"access_token": "x"})

    def test_an_id_token_for_a_different_subject_is_refused(self):
        """The decisive check: the signed token and the token response must name one person."""
        from .orcid_oauth import ORCIDAuthError
        self.start()
        from . import orcid_oauth
        nonce = self.session[orcid_oauth.NONCE_KEY]
        with self.assertRaises(ORCIDAuthError):
            self.complete({"orcid": DEPOSITOR_ORCID,
                           "id_token": self.id_token(sub=OTHER_ORCID, nonce=nonce)})

    def test_an_id_token_with_the_wrong_nonce_is_refused(self):
        """A replayed id_token from another login must not be accepted."""
        from .orcid_oauth import ORCIDAuthError
        self.start()
        with self.assertRaises(ORCIDAuthError):
            self.complete({"orcid": DEPOSITOR_ORCID,
                           "id_token": self.id_token(nonce="some-other-nonce")})

    def test_an_id_token_for_another_audience_is_refused(self):
        from .orcid_oauth import ORCIDAuthError
        self.start()
        from . import orcid_oauth
        nonce = self.session[orcid_oauth.NONCE_KEY]
        with self.assertRaises(ORCIDAuthError):
            self.complete({"orcid": DEPOSITOR_ORCID,
                           "id_token": self.id_token(nonce=nonce, audience="APP-SOMEONEELSE")})


@override_settings(**ORCID_SETTINGS)
class DepositorAccessTests(EmbargoBase):
    """What signing in actually grants, which is a third route to embargoed data."""

    def setUp(self):
        super().setUp()
        self.other = Depositor.objects.create(name="Someone Else", orcid=OTHER_ORCID)
        self.others_entry = Deposition.objects.create(
            accession="9QQQ", title="Another embargoed structure", depositor=self.other,
            deposited_on=timezone.localdate(),
            release_on=timezone.localdate() + timedelta(days=60))

    def sign_in(self, orcid=DEPOSITOR_ORCID):
        from .orcid_oauth import SESSION_ORCID
        session = self.client.session
        session[SESSION_ORCID] = orcid
        session.save()

    def test_depositor_sees_their_own_embargoed_entry(self):
        self.sign_in()
        response = self.get("/api/depositions/9XYZ/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AccessRecord.objects.first().outcome,
                         AccessRecord.Outcome.GRANTED_DEPOSITOR)

    def test_depositor_cannot_see_someone_elses_embargoed_entry(self):
        """Signing in is not a skeleton key, and the refusal is still a 404."""
        self.sign_in()
        self.assertEqual(self.get("/api/depositions/9QQQ/").status_code, 404)
        self.assertEqual(AccessRecord.objects.first().outcome,
                         AccessRecord.Outcome.DENIED_NOT_DEPOSITOR)

    def test_public_list_still_hides_embargoed_entries_from_their_own_depositor(self):
        self.sign_in()
        accessions = [row["accession"] for row in self.get("/api/depositions/").json()]
        self.assertNotIn("9XYZ", accessions)

    def test_mine_returns_own_entries_including_embargoed(self):
        self.sign_in()
        accessions = [row["accession"] for row in self.get("/api/mine/").json()]
        self.assertIn("9XYZ", accessions)
        self.assertIn("1ABC", accessions)
        self.assertNotIn("9QQQ", accessions)

    def test_mine_requires_signing_in(self):
        self.assertEqual(self.get("/api/mine/").status_code, 401)

    def test_logout_ends_the_access(self):
        self.sign_in()
        self.assertEqual(self.get("/api/depositions/9XYZ/").status_code, 200)
        self.client.get("/auth/orcid/logout/")
        self.assertEqual(self.get("/api/depositions/9XYZ/").status_code, 404)

    def test_deleting_the_depositor_revokes_access_immediately(self):
        """The session holds an iD, not a row id, so the row is resolved on every request."""
        self.sign_in()
        self.assertEqual(self.get("/api/depositions/9XYZ/").status_code, 200)
        self.embargoed.depositor = self.other
        self.embargoed.save(update_fields=["depositor"])
        self.assertEqual(self.get("/api/depositions/9XYZ/").status_code, 404)

    def test_a_signed_in_id_with_no_depositor_row_grants_nothing(self):
        self.sign_in(orcid="0009-0000-9103-975X")
        self.assertEqual(self.get("/api/depositions/9XYZ/").status_code, 404)
        self.assertEqual(self.get("/api/mine/").status_code, 401)

    def test_callback_with_an_unknown_id_does_not_create_a_depositor(self):
        """Depositing makes someone a depositor. Signing in must not."""
        from unittest import mock

        from . import orcid_oauth, views
        before = Depositor.objects.count()
        with mock.patch.object(views.orcid_oauth, "complete", return_value="0009-0000-9103-975X"):
            response = self.client.get("/auth/orcid/callback/?code=x&state=y")
        self.assertEqual(Depositor.objects.count(), before)
        self.assertNotIn(orcid_oauth.SESSION_ORCID, self.client.session)
        self.assertIn("signin=unknown", response.url)

    def test_callback_failure_reveals_nothing_about_which_step_failed(self):
        from unittest import mock

        from . import views
        with mock.patch.object(views.orcid_oauth, "complete",
                               side_effect=views.orcid_oauth.ORCIDAuthError("state mismatch")):
            response = self.client.get("/auth/orcid/callback/?code=x&state=y")
        self.assertIn("signin=failed", response.url)
        self.assertNotIn("state", response.url)


# Another hand-written fragment, not a real entry. Its author iD matches DEPOSITOR_ORCID so the
# authorship check can pass, and its accession does not collide with the seeded ones.
UPLOAD_CIF = """data_7NEW
#
_entry.id   7NEW
_struct.title   'A newly deposited structure'
_exptl.method   'X-RAY DIFFRACTION'
_refine.ls_d_res_high   1.95
#
loop_
_audit_author.name
_audit_author.identifier_ORCID
'Smith, J.'   %s
#
""" % DEPOSITOR_ORCID


@override_settings(**ORCID_SETTINGS)
class DepositUploadTests(EmbargoBase):
    """Accepting a deposition from its own author."""

    def sign_in(self, orcid=DEPOSITOR_ORCID):
        from .orcid_oauth import SESSION_ORCID
        session = self.client.session
        session[SESSION_ORCID] = orcid
        session.save()

    def upload(self, text=UPLOAD_CIF, release_in_days=90, name="upload.cif", client=None):
        from django.core.files.uploadedfile import SimpleUploadedFile
        payload = text if isinstance(text, bytes) else text.encode()
        return (client or self.client).post("/api/deposit/", {
            "coordinates": SimpleUploadedFile(name, payload),
            "release_on": (timezone.localdate() + timedelta(days=release_in_days)).isoformat(),
        })

    def test_upload_requires_signing_in(self):
        self.assertEqual(self.upload().status_code, 401)

    def test_a_valid_upload_is_accepted(self):
        self.sign_in()
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["accession"], "7NEW")

    def test_the_record_is_built_from_the_file_not_from_the_request(self):
        """The depositor chooses the release date and nothing else, so the two cannot disagree."""
        self.sign_in()
        self.upload()
        created = Deposition.objects.get(accession="7NEW")
        self.assertEqual(created.title, "A newly deposited structure")
        self.assertEqual(created.method, Deposition.Method.XRAY)
        self.assertEqual(created.resolution, 1.95)
        self.assertEqual(created.depositor, self.depositor)
        self.assertEqual(created.verify_coordinates(), [])

    def test_an_uploader_who_is_not_an_author_is_refused(self):
        """The check that stops a depositor claiming someone else's structure."""
        other = Depositor.objects.create(name="Someone Else", orcid=OTHER_ORCID)
        self.sign_in(orcid=other.orcid)
        response = self.upload()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Deposition.objects.filter(accession="7NEW").exists())
        self.assertEqual(AccessRecord.objects.first().outcome,
                         AccessRecord.Outcome.DENIED_NOT_AUTHOR)

    def test_a_file_naming_no_orcid_at_all_is_refused(self):
        """Authorship cannot be established from a name, so we do not pretend otherwise."""
        self.sign_in()
        response = self.upload(UPLOAD_CIF.replace(DEPOSITOR_ORCID, "?"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Deposition.objects.filter(accession="7NEW").exists())

    def test_a_release_date_in_the_past_is_refused(self):
        self.sign_in()
        self.assertEqual(self.upload(release_in_days=-1).status_code, 400)

    def test_a_release_date_of_today_is_refused(self):
        """Releasing today is not an embargo, it is a public deposition."""
        self.sign_in()
        self.assertEqual(self.upload(release_in_days=0).status_code, 400)

    def test_a_duplicate_accession_is_refused_rather_than_overwritten(self):
        """Overwriting would lose the file a reviewer may already be reading."""
        self.sign_in()
        self.assertEqual(self.upload().status_code, 201)
        self.assertEqual(self.upload().status_code, 400)
        self.assertEqual(Deposition.objects.filter(accession="7NEW").count(), 1)

    def test_a_file_that_is_not_mmcif_is_refused(self):
        self.sign_in()
        self.assertEqual(self.upload("this is not a structure").status_code, 400)

    def test_a_file_with_no_entry_id_is_refused(self):
        self.sign_in()
        self.assertEqual(self.upload(UPLOAD_CIF.replace("_entry.id   7NEW", "")).status_code, 400)

    def test_a_file_that_is_not_text_is_refused(self):
        self.sign_in()
        self.assertEqual(self.upload(b"\xff\xfe\x00binary").status_code, 400)

    @override_settings(MAX_UPLOAD_BYTES=64)
    def test_an_oversized_file_is_refused(self):
        """An upload endpoint with no ceiling is a way to fill the disk."""
        self.sign_in()
        self.assertEqual(self.upload().status_code, 400)

    def test_a_new_deposition_is_embargoed_and_invisible_to_the_public(self):
        self.sign_in()
        self.upload()
        self.client.logout()
        self.assertEqual(self.get("/api/depositions/7NEW/").status_code, 404)
        self.assertNotIn("7NEW", [r["accession"] for r in self.get("/api/depositions/").json()])

    def test_the_deposit_is_recorded(self):
        self.sign_in()
        self.upload()
        record = AccessRecord.objects.first()
        self.assertEqual(record.outcome, AccessRecord.Outcome.DEPOSITED)
        self.assertEqual(record.accession, "7NEW")

    def test_csrf_is_enforced_on_upload(self):
        """DRF marks APIViews csrf_exempt, and a depositor here is a session cookie. Without
        csrf_protect any site could make a signed-in depositor deposit a file."""
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        session = strict.session
        from .orcid_oauth import SESSION_ORCID
        session[SESSION_ORCID] = DEPOSITOR_ORCID
        session.save()
        self.assertEqual(self.upload(client=strict).status_code, 403)


@override_settings(**ORCID_SETTINGS)
class ReleaseTests(EmbargoBase):
    """Lifting an embargo early, which closes the deposit-review-release loop."""

    def sign_in(self, orcid=DEPOSITOR_ORCID):
        from .orcid_oauth import SESSION_ORCID
        session = self.client.session
        session[SESSION_ORCID] = orcid
        session.save()

    def test_a_depositor_can_release_their_own_entry(self):
        self.sign_in()
        self.assertEqual(self.client.post("/api/depositions/9XYZ/release/").status_code, 200)
        self.embargoed.refresh_from_db()
        self.assertFalse(self.embargoed.is_embargoed())

    def test_releasing_makes_it_public_without_a_token(self):
        self.sign_in()
        self.client.post("/api/depositions/9XYZ/release/")
        self.client.logout()
        self.assertEqual(self.get("/api/depositions/9XYZ/").status_code, 200)
        self.assertIn("9XYZ", [r["accession"] for r in self.get("/api/depositions/").json()])

    def test_release_is_recorded(self):
        self.sign_in()
        self.client.post("/api/depositions/9XYZ/release/")
        self.assertEqual(AccessRecord.objects.first().outcome, AccessRecord.Outcome.RELEASED)

    def test_someone_elses_entry_cannot_be_released(self):
        other = Depositor.objects.create(name="Someone Else", orcid=OTHER_ORCID)
        self.sign_in(orcid=other.orcid)
        self.assertEqual(self.client.post("/api/depositions/9XYZ/release/").status_code, 404)
        self.embargoed.refresh_from_db()
        self.assertTrue(self.embargoed.is_embargoed())

    def test_release_requires_signing_in(self):
        self.assertEqual(self.client.post("/api/depositions/9XYZ/release/").status_code, 401)
