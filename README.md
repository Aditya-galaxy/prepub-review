# prepub-review

[![tests](https://github.com/Aditya-galaxy/prepub-review/actions/workflows/ci.yml/badge.svg)](https://github.com/Aditya-galaxy/prepub-review/actions/workflows/ci.yml)

**A structural biology deposition is confidential until its release date. This is a Django service
that lets a journal's reviewers read one embargoed entry, for a limited time, and records every
attempt to reach it.**

The problem is the one the worldwide Protein Data Bank has to solve. Depositors send coordinates
and metadata long before the paper that describes them is published, and the archive has to honour
that embargo while still letting the reviewers of the manuscript see the data they are being asked
to assess. Access has to be narrow, revocable, time limited, and auditable afterwards.

## The rule being enforced

An entry is public once `release_on` has passed. Before that it is reachable only by someone
presenting a token that was issued for that exact entry, and only while the token is live.

| Request | Answer |
|---|---|
| Released entry, no token | 200 |
| Embargoed entry, no token | **404** |
| Embargoed entry, token for a different entry | **404** |
| Embargoed entry, its own valid token | 200 |
| Any entry, expired, revoked or unknown token | 401 |
| Embargoed entry, its depositor signed in with ORCID | 200 |
| Embargoed entry, a *different* depositor signed in | **404** |
| Embargoed entry in the public list | never, for anyone |
| Depositing, releasing, or anything else with a session cookie | CSRF token required |

**The 404 is deliberate and is the most important decision here.** A 403 would confirm that the
accession exists and is under embargo, and that is itself information about unpublished work: it
tells a competitor that a particular group has deposited a particular structure. So the service
declines to distinguish "does not exist" from "exists and you may not see it". The audit trail
still records the real reason, so the operator loses nothing.

## What is real here, and what is not

Every field describing an entry comes from the RCSB Data API: accession, title, experimental
method, resolution, deposition date, release date, and the depositing authors with their ORCID iDs
where the entry records one. The selection query is in `review/management/commands/seed_from_pdb.py`
and is copied into `seed_manifest.json` on every run, so the set can be rebuilt.

**The embargo dates are not real, and cannot be.** An entry genuinely under embargo is not public,
so no public API will serve one. To demonstrate the embargo at all, `--embargo N` moves the release
date of N entries into the future. Those rows carry `embargo_simulated=True`, the flag shows in the
admin list, and the manifest records which accessions were changed and why. Nothing in this
repository presents a simulated date as a measured one.

Depositors with no ORCID iD in the source entry are stored with none. Generating a plausible iD to
fill the column would be inventing an identifier for a real person.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_from_pdb --count 10 --embargo 3   # real PDB metadata
.venv/bin/python manage.py issue_token 11CX --for "J. Struct. Biol. / JSB-2026-0417" --days 14
.venv/bin/python manage.py runserver
```

Then open `http://127.0.0.1:8000/` and paste the token. `manage.py createsuperuser` gets you into
`/admin/`, where tokens can be issued and revoked and the audit trail can be read but not altered.

On macOS a Python installed outside Homebrew may have no CA bundle, which makes the seed command
fail with `CERTIFICATE_VERIFY_FAILED`. Fix it for the session with
`export SSL_CERT_FILE=$(python -m certifi)`.

PostgreSQL and gunicorn, as deployed:

```bash
docker compose up --build
```

SQLite is the default only so that a fresh checkout runs with nothing installed. Set `DATABASE_URL`
to a `postgresql://` URL and the same code uses PostgreSQL.

## The API

| Endpoint | Purpose |
|---|---|
| `GET /api/depositions/` | Released entries. Never includes an embargoed one. |
| `GET /api/depositions/<accession>/` | One entry, subject to the table above. |
| `GET /api/depositions/<accession>/coordinates/` | The coordinate file, same check applied again. |
| `GET /api/session/` | What the presented token opens, when it expires, accesses left. |
| `GET /api/mine/` | The signed-in depositor's own entries, embargoed included. |
| `POST /api/deposit/` | Upload an mmCIF file and a release date. |
| `POST /api/depositions/<accession>/release/` | Lift your own embargo early. |
| `GET /auth/orcid/login/` \| `/callback/` \| `/logout/` | The ORCID sign-in flow. |

Credentials go in `Authorization: Review <token>`.

```bash
curl -H "Authorization: Review $TOKEN" http://127.0.0.1:8000/api/depositions/11CX/
```

**Tokens are issued through the admin and the `issue_token` command, not through this API.** Keeping
the issuing path off the reviewer-facing surface means a flaw in the read path cannot be turned
into a way to mint access.

## Depositing

A depositor signs in, uploads an mmCIF file, and chooses when the embargo ends. That is the whole
form: `review/serializers.py` takes a file and a date and nothing else.

```
sign in with ORCID -> upload mmCIF -> embargoed -> issue a review token
                   -> reviewer reads that one entry -> release
```

Three rules make it safe, and they are the reason the form is that short.

**The record is built from the file, never typed alongside it.** Accession, title, method and
resolution are read out of the header, so the record and the file cannot disagree at the moment
the record is created. Verification then only has to catch a later re-upload.

**The uploader has to be named in the file**, matched on ORCID iD against
`_audit_author.identifier_ORCID`. If the file records no ORCID iD for any author, the upload is
refused rather than waved through: authorship cannot be established from a name, and a deposition
system that accepts "trust me" at this point has no access control worth the name. Real archives
have other evidence available to them; this service does not, and says so instead of pretending.

**The release date has to be in the future.** A release date of today is not an embargo, it is a
public deposition, so it is rejected rather than quietly accepted.

A duplicate accession is refused rather than overwritten, because overwriting would destroy the
file a reviewer may be part-way through reading. Replacing a deposition is versioning, which this
service does not do.

A depositor can lift their own embargo early, which is the only way an entry becomes public other
than its date arriving. Someone else's entry returns the same 404 as everywhere else.

**One detail worth naming: `csrf_protect` is applied explicitly.** DRF marks `APIView`
`csrf_exempt`, and a depositor here is identified by a session cookie, so without it any site
could make a signed-in depositor deposit a file or lift their own embargo. The page sets the
cookie with `ensure_csrf_cookie` so the first upload of a session is not refused for want of a
token that was never issued.

## Two principals, two mechanisms

A reviewer and a depositor are different people with different claims on an entry, so they
authenticate differently, which is how a real deposition system works.

A **reviewer** holds a bearer token issued by the journal. They are deliberately anonymous: the
token names the manuscript, not the person.

A **depositor** is a named person who already has an ORCID iD, and signs in with it to see their
own work, embargoed included. `review/orcid_oauth.py` implements the OAuth2 authorisation-code
flow with OpenID Connect, against **ORCID's sandbox by default**, because reaching the real
registry should be a deliberate act rather than the result of a forgotten variable.

```bash
export ORCID_CLIENT_ID=APP-XXXXXXXXXXXXXXXX ORCID_CLIENT_SECRET=...
export ORCID_REDIRECT_URI=http://127.0.0.1:8000/auth/orcid/callback/
```

What the flow checks, and why each one is there:

- **`state`**, random and single use. It is removed from the session before anything else happens,
  so a replayed callback fails. Without it, any site could start a sign-in in a visitor's browser.
- **`nonce`**, carried into the `id_token` and compared on return, so an `id_token` captured from
  a different login cannot be replayed into this one.
- **The `id_token` signature**, verified against ORCID's published JWKS with issuer, audience and
  expiry all enforced.
- **Agreement between the two sources of the iD.** The token response and the verified `id_token`
  must name the same person. If they disagree the sign-in is refused, rather than resolved in
  favour of whichever is more convenient.
- **The session key is cycled** on sign-in, so a session fixed beforehand is not the one that ends
  up carrying an identity.

Signing in creates nothing. A person becomes a depositor by depositing, so an ORCID iD with no
depositions is told exactly that and no row is written. Every sign-in failure redirects to the
same place with the same message; the reason is logged for the operator but not shown, because
naming the failing step helps someone probing the flow more than it helps a depositor who needs
to try again.

The session holds the iD, not a row id, and the row is resolved on every request, so removing a
depositor takes effect at once rather than whenever their session expires.

## Checking a file against its record

A deposition system has to answer a curation question before release: does the uploaded file say
what the record says? `review/mmcif.py` is a small mmCIF reader that pulls the entry id, title,
experimental method, resolution and audit authors with their ORCID iDs, and
`Deposition.verify_coordinates()` compares them.

```bash
.venv/bin/python manage.py seed_from_pdb --count 6 --embargo 2 --with-files
.venv/bin/python manage.py check_coordinates
```

It is not a general CIF parser and does not claim to be. It handles what archive files actually
contain: single and double quoted values, semicolon text fields, `loop_` constructs, the `.` and
`?` placeholders, and comments. It does not handle save frames, multiple data blocks or CIF 2.0,
which deposition files do not use. Three details are worth naming, because a parser that gets them
wrong fails quietly rather than loudly:

- A quote only closes a value when whitespace follows it, so `"O'Brien, S."` stays intact. A naive
  split on the quote character turns that author into `O`.
- A semicolon starts a text field only at the beginning of a line, so a semicolon inside a title
  does not end the value early.
- `.` and `?` mean "not applicable" and "unknown". They become `None`, never the strings.

Checked against real archive files, including an 8 MB cryo-EM entry, where the resolution has to
come from `_em_3d_reconstruction.resolution` because a cryo-EM entry has no `_refine` category at
all. A mismatch is usually a re-uploaded wrong file or a title edited in one place and not the
other, so the command reports the record and the file side by side rather than just failing.

## Design decisions worth defending

**Tokens are stored as HMAC digests, never in clear.** A copy of the database must not hand its
reader working access to unpublished science. HMAC rather than a bare SHA-256 so that someone
holding the table still cannot test candidate tokens offline without the pepper, which lives in
`PREPUB_TOKEN_PEPPER` and is deliberately separate from `SECRET_KEY`. A short prefix is kept in
clear so an audit record can be tied to a token without the record containing a usable secret.

**The audit trail is written in middleware, not in the views.** A view that forgets to log is a
hole in the trail, and the request that most needs recording is the one rejected before any view
ran. `review/middleware.py` writes one record per `/api/` request on every path through the
application, including authentication failures and 404s. Where authentication rejects a token
before URL handling finishes, the accession is recovered from the resolved route, so the record
for the most suspicious request still says what was being targeted.

**`AccessRecord` is append-only in the model, not only by convention.** `save()` refuses to update
an existing row and `delete()` raises. That alone is not enough, because queryset `update()` and
`delete()` compile straight to SQL and never call either method, so a custom queryset closes
those too: without it, one line could quietly empty the trail. The admin registers the model
with add, change and delete permissions all denied.

**The audit trail denormalises what it needs.** `accession` and `token_prefix` are stored as text
rather than as foreign keys. A trail that loses its rows when a token is deleted is not a trail,
and the question it has to answer later, who reached this entry and when, has to survive the entry
and the token being removed.

**Client addresses are hashed with a keyed digest.** An IP address identifies a person under the
GDPR. The audit only ever needs to answer "was this the same client?", and a keyed digest answers
that without storing a personal identifier. Reviewer identities are not collected at all:
`issued_for` names the journal and the manuscript, because peer review is normally confidential and
the service has no use for the name.

**Coordinate files are served by a view, never by the web server.** There is no `MEDIA_URL` and no
static route to `MEDIA_ROOT`. Exposing that directory to nginx would let anyone who guesses a
filename walk straight past the embargo.

**ORCID iDs are checksummed, not regex matched.** `review/orcid.py` implements the ISO 7064
MOD 11-2 check digit, so a transposed digit is caught at entry instead of becoming a silently wrong
attribution. It accepts bare iDs and `https://orcid.org/` URIs, and handles the `X` check character.

**Access counting uses `F()`.** Two reviewers using one token at the same moment must not both read
the same count and overshoot the limit.

## Tests

```bash
.venv/bin/python manage.py test review
```

88 tests, covering the embargo table above, the token lifecycle (expiry, revocation, access
limits, unknown tokens), the ORCID check digit, and the audit trail: that refusals are recorded,
that the plaintext token never reaches the database, that the client address is not stored in
clear, that records cannot be modified or deleted either one at a time or in bulk, and that the trail
survives deletion of the token it refers to. The deposit tests cover the three rules above plus the refusals: a non-author, a file naming
no ORCID iD, a past or same-day release date, a duplicate accession, an unparseable or
non-text file, an oversized one, and a request without a CSRF token. The ORCID tests sign a
real RS256 `id_token` with a throwaway key and check that a wrong
subject, a wrong nonce, a wrong audience, a mismatched `state` and a replayed callback are
each refused, and that signing in opens your own embargoed entry and not anyone else's. The
mmCIF tests use a hand-written fragment,
labelled as fake, that exercises the syntax quirks above, and check that each kind of
record-versus-file mismatch is caught rather than only that a matching file passes.

Each test names the rule it defends in its docstring, because a test whose purpose is not obvious
gets deleted by the next person who sees it fail.

## Limits

This is a working model, not a deposition system. The loop runs end to end, but there is no
curation workflow and no versioning: a deposition cannot be replaced, corrected or superseded,
and nobody reviews it between deposit and release. The header check compares metadata rather
than validating the coordinates themselves, which is a much larger job.

Authorship is established only from ORCID iDs in the file, so a file whose authors recorded
none cannot be deposited here at all. That is the safe failure, not a good one. Real
pre-publication access at the wwPDB is coordinated across several independent sites, and nothing
here addresses that.

Reviewer authentication is a bearer token, so anyone holding it has access. That is the right
shape for reviewers, who are anonymous to the depositor by design, but it means a leaked token
is live until revoked or expired. The access limit and the short default lifetime narrow that
window rather than closing it.

ORCID sign-in covers depositors but not reviewers. A journal that wanted to tie a token to an
identified reviewer could reuse the same flow, and the token model would need a nullable link
to a person. It has been left out because anonymous review is the normal case.

The audit trail is append-only against ordinary application mistakes, not against someone with
direct database access. Making it tamper-evident would need hash chaining or an append-only store.

## Licence

Code: MIT. Entry metadata comes from the RCSB PDB under the terms of the PDB, which places no
restrictions on its use.
