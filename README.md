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
| Embargoed entry in the public list | never, with or without a token |

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

Credentials go in `Authorization: Review <token>`.

```bash
curl -H "Authorization: Review $TOKEN" http://127.0.0.1:8000/api/depositions/11CX/
```

**Tokens are issued through the admin and the `issue_token` command, not through this API.** Keeping
the issuing path off the reviewer-facing surface means a flaw in the read path cannot be turned
into a way to mint access.

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

48 tests, covering the embargo table above, the token lifecycle (expiry, revocation, access
limits, unknown tokens), the ORCID check digit, and the audit trail: that refusals are recorded,
that the plaintext token never reaches the database, that the client address is not stored in
clear, that records cannot be modified or deleted either one at a time or in bulk, and that the trail
survives deletion of the token it refers to. The mmCIF tests use a hand-written fragment,
labelled as fake, that exercises the syntax quirks above, and check that each kind of
record-versus-file mismatch is caught rather than only that a matching file passes.

Each test names the rule it defends in its docstring, because a test whose purpose is not obvious
gets deleted by the next person who sees it fail.

## Limits

This is a working model of one mechanism, not a deposition system. It has no upload path and no
versioning of depositions, and the header check compares metadata rather than validating the
coordinates themselves, which is a much larger job. Real
pre-publication access at the wwPDB is coordinated across several independent sites, and nothing
here addresses that.

Authentication is a bearer token, so anyone holding it has access. That is the right shape for
reviewers, who are anonymous to the depositor by design, but it means a leaked token is live until
revoked or expired. The access limit and the short default lifetime narrow that window rather than
closing it. ORCID sign-in, which would let a journal tie a token to an identified reviewer where
that is wanted, is in the data model but not implemented.

The audit trail is append-only against ordinary application mistakes, not against someone with
direct database access. Making it tamper-evident would need hash chaining or an append-only store.

## Licence

Code: MIT. Entry metadata comes from the RCSB PDB under the terms of the PDB, which places no
restrictions on its use.
