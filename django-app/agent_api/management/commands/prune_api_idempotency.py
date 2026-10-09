from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from agent_api.models import IdempotencyRecord


class Command(BaseCommand):
    help = "Delete finished API retry records older than seven days; retain unknown dispatches for review."

    def handle(self, **options):
        count, _ = IdempotencyRecord.objects.filter(state="complete", created_at__lt=timezone.now() - timedelta(days=7)).delete()
        self.stdout.write(f"Deleted {count} completed retry records.")
