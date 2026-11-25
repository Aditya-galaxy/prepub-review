"""Seed the database from real PDB entries, by a fixed query rather than by hand.

Everything this command stores about an entry is real and comes from the RCSB Data API:
accession, title, experimental method, resolution, deposition date, release date, and the
depositing authors with their ORCID iDs where the entry records one.

One thing is not real, and cannot be. An entry genuinely under embargo is not public, so no public
API will serve one. To demonstrate the embargo at all, `--embargo N` moves the release date of N
entries into the future and sets `embargo_simulated=True` on them. The flag is stored on the row,
printed in the summary, and written into the manifest, so no reader can mistake a demonstration
date for a measured one.
"""
import json
import random
import urllib.error
import urllib.request
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from review.models import Deposition, Depositor
from review.orcid import InvalidORCID, normalise

SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
ENTRY_URL = "https://data.rcsb.org/rest/v1/core/entry/{pdb_id}"
USER_AGENT = "prepub-review/0.1 (student project; contact via repository)"

# Recorded here, and copied into the manifest, so the set can be rebuilt exactly.
QUERY = {
    "query": {"type": "group", "logical_operator": "and", "nodes": [
        {"type": "terminal", "service": "text", "parameters": {
            "attribute": "rcsb_entry_info.polymer_entity_count_protein",
            "operator": "equals", "value": 1}},
        {"type": "terminal", "service": "text", "parameters": {
            "attribute": "rcsb_accession_info.initial_release_date",
            "operator": "greater", "value": "2023-01-01T00:00:00Z"}},
    ]},
    "return_type": "entry",
    "request_options": {"paginate": {"start": 0, "rows": 400}},
}


def _get_json(url, data=None, timeout=60):
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def search_ids():
    payload = _get_json(SEARCH_URL, data=QUERY)
    return [hit["identifier"] for hit in payload.get("result_set", [])]


def entry_metadata(pdb_id):
    return _get_json(ENTRY_URL.format(pdb_id=pdb_id))


def _date(value):
    """RCSB returns ISO timestamps; keep only the date."""
    return value.split("T")[0] if value else None


def _method(entry):
    methods = entry.get("exptl") or []
    raw = (methods[0].get("method") if methods else "") or ""
    for choice in Deposition.Method:
        if choice.value == raw.upper():
            return choice.value
    return Deposition.Method.OTHER


class Command(BaseCommand):
    help = "Load real PDB entry metadata, optionally simulating an embargo on some of them."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=12, help="entries to load")
        parser.add_argument("--embargo", type=int, default=4,
                            help="how many of them to place under a simulated embargo")
        parser.add_argument("--seed", type=int, default=0, help="sampling seed")
        parser.add_argument("--manifest", default="seed_manifest.json")

    def handle(self, *args, **opts):
        count, n_embargo = opts["count"], opts["embargo"]
        if n_embargo > count:
            raise CommandError("--embargo cannot exceed --count")

        self.stdout.write("querying RCSB...")
        try:
            all_ids = search_ids()
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise CommandError(f"RCSB search failed: {exc}") from exc
        if not all_ids:
            raise CommandError("RCSB search returned no entries")

        chosen = sorted(random.Random(opts["seed"]).sample(sorted(all_ids), min(count, len(all_ids))))
        self.stdout.write(f"{len(all_ids)} entries matched; loading {len(chosen)}")

        loaded, skipped = [], []
        for pdb_id in chosen:
            try:
                entry = entry_metadata(pdb_id)
            except Exception as exc:                               # noqa: BLE001
                skipped.append((pdb_id, type(exc).__name__))
                continue

            accession_info = entry.get("rcsb_accession_info") or {}
            deposited = _date(accession_info.get("deposit_date"))
            released = _date(accession_info.get("initial_release_date"))
            if not (deposited and released):
                skipped.append((pdb_id, "missing dates"))
                continue

            depositor = self._depositor(entry)
            resolutions = (entry.get("rcsb_entry_info") or {}).get("resolution_combined") or []
            struct = entry.get("struct") or {}

            deposition, _ = Deposition.objects.update_or_create(
                accession=pdb_id.upper(),
                defaults={
                    "title": (struct.get("title") or pdb_id)[:400],
                    "method": _method(entry),
                    "resolution": resolutions[0] if resolutions else None,
                    "depositor": depositor,
                    "deposited_on": deposited,
                    "release_on": released,
                    "embargo_simulated": False,
                })
            loaded.append(deposition)

        if not loaded:
            raise CommandError("nothing could be loaded; see the skipped list above")

        embargoed = self._simulate_embargo(loaded, n_embargo, opts["seed"])

        manifest = {
            "query": QUERY,
            "matched_entries": len(all_ids),
            "requested": count,
            "seed": opts["seed"],
            "loaded": [d.accession for d in loaded],
            "skipped": skipped,
            "simulated_embargo": {
                "accessions": [d.accession for d in embargoed],
                "note": "release_on for these rows was moved into the future to demonstrate the "
                        "embargo. Entries genuinely under embargo are not public, so a real "
                        "pre-release date cannot be obtained. Every other field is from RCSB.",
            },
        }
        with open(opts["manifest"], "w") as fh:
            json.dump(manifest, fh, indent=2)

        self.stdout.write(self.style.SUCCESS(
            f"loaded {len(loaded)} entries, {len(embargoed)} under a simulated embargo"))
        if skipped:
            self.stdout.write(f"skipped {len(skipped)}: {skipped}")
        self.stdout.write(f"manifest written to {opts['manifest']}")

    @staticmethod
    def _depositor(entry):
        """First audit author, with their ORCID iD only if the entry actually records one."""
        authors = entry.get("audit_author") or []
        name = (authors[0].get("name") if authors else "") or "Unknown depositor"
        orcid = None
        for author in authors:
            candidate = author.get("identifier_ORCID")
            if candidate:
                try:
                    orcid = normalise(candidate)
                except InvalidORCID:
                    orcid = None
                if orcid:
                    break
        if orcid:
            depositor, _ = Depositor.objects.get_or_create(orcid=orcid, defaults={"name": name[:200]})
            return depositor
        depositor = Depositor.objects.filter(name=name[:200], orcid__isnull=True).first()
        return depositor or Depositor.objects.create(name=name[:200], orcid=None)

    def _simulate_embargo(self, loaded, n_embargo, seed):
        if n_embargo <= 0:
            return []
        picked = random.Random(seed).sample(loaded, min(n_embargo, len(loaded)))
        today = timezone.localdate()
        for offset, deposition in enumerate(picked, start=1):
            deposition.release_on = today + timedelta(days=30 * offset)
            deposition.embargo_simulated = True
            deposition.save(update_fields=["release_on", "embargo_simulated"])
        return picked
