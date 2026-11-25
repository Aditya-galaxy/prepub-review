"""Check deposited files against their records, the way a curator would before release."""
from django.core.management.base import BaseCommand

from review.mmcif import MMCIFError
from review.models import Deposition


class Command(BaseCommand):
    help = "Compare each deposition's coordinate file header against its stored metadata."

    def add_arguments(self, parser):
        parser.add_argument("accession", nargs="?", help="one entry, or all of them if omitted")

    def handle(self, *args, **opts):
        query = Deposition.objects.select_related("depositor")
        if opts["accession"]:
            query = query.filter(accession=opts["accession"].upper())

        checked = agreed = 0
        for deposition in query:
            if not deposition.coordinates:
                self.stdout.write(f"{deposition.accession}: no file")
                continue
            checked += 1
            try:
                found = deposition.verify_coordinates()
            except MMCIFError as exc:
                self.stdout.write(self.style.ERROR(f"{deposition.accession}: unreadable, {exc}"))
                continue
            if not found:
                agreed += 1
                self.stdout.write(self.style.SUCCESS(f"{deposition.accession}: agrees"))
                continue
            self.stdout.write(self.style.WARNING(f"{deposition.accession}: {len(found)} mismatch(es)"))
            for field_name, in_record, in_file in found:
                self.stdout.write(f"    {field_name}:")
                self.stdout.write(f"      record: {in_record}")
                self.stdout.write(f"      file:   {in_file}")

        self.stdout.write(f"\n{agreed} of {checked} file(s) agree with their record")
