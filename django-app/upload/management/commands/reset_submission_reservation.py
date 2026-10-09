from django.core.management.base import BaseCommand, CommandError
from upload.models import SubmissionReservation, ProcessingTask
from upload.submission import ACTIVE


class Command(BaseCommand):
    help = "Reset an ambiguous upload reservation ONLY after reconciling the internal processor."

    def add_arguments(self, parser):
        parser.add_argument("--confirmed-backend-idle", action="store_true")

    def handle(self, **options):
        if not options["confirmed_backend_idle"]:
            raise CommandError("First reconcile the backend and confirm no processing remains; pass --confirmed-backend-idle.")
        if ProcessingTask.objects.filter(status__in=ACTIVE).exists():
            raise CommandError("An active local task still exists; reconcile it first.")
        SubmissionReservation.objects.filter(pk=1).update(token=None, task=None)
        self.stdout.write("Submission reservation reset; unknown retry records still require manual reconciliation.")
