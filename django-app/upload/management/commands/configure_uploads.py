from django.core.management.base import BaseCommand
from upload.configuration import seed
from upload.ingestion import cleanup


class Command(BaseCommand):
    help = 'Seed missing PRIME upload settings and/or delete expired private previews.'

    def add_arguments(self, parser):
        parser.add_argument('--cleanup-only', action='store_true')

    def handle(self, *args, **options):
        cleanup()
        if not options['cleanup_only']:
            seed()
        self.stdout.write('Upload configuration/preview maintenance complete.')
