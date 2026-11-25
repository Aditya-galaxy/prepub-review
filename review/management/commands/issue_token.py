"""Issue a review token from the command line. The token is printed once and never stored."""
from django.core.management.base import BaseCommand, CommandError

from review.models import Deposition, ReviewToken


class Command(BaseCommand):
    help = "Issue a review token for one deposition."

    def add_arguments(self, parser):
        parser.add_argument("accession")
        parser.add_argument("--for", dest="issued_for", required=True,
                            help="Journal and manuscript reference, not the reviewer's name")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--max-accesses", type=int, default=0, help="0 means no limit")

    def handle(self, *args, **opts):
        deposition = Deposition.objects.filter(accession=opts["accession"].upper()).first()
        if deposition is None:
            raise CommandError(f"no deposition {opts['accession'].upper()}")
        token, plaintext = ReviewToken.issue(
            deposition, issued_for=opts["issued_for"],
            valid_for_days=opts["days"], max_accesses=opts["max_accesses"])
        self.stdout.write(self.style.SUCCESS(f"token for {deposition.accession}: {plaintext}"))
        self.stdout.write(f"expires {token.expires_at:%Y-%m-%d %H:%M} UTC. "
                          f"It is stored only as a hash and cannot be shown again.")
