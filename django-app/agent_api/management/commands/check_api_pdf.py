import os
from django.core.management.base import BaseCommand, CommandError
from agent_api.auth import APIError
from agent_api.pdf import render_pdf


class Command(BaseCommand):
    help = "Render synthetic PDF bytes to verify Chromium sandbox and runtime dependencies."

    def handle(self, **options):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise CommandError("Run this check as the application user, not root.")
        try:
            content = render_pdf("<html><body><h1>PRIMER synthetic PDF runtime check</h1></body></html>")
        except APIError as exc:
            raise CommandError(exc.code) from None
        self.stdout.write(f"Synthetic PDF rendered successfully ({len(content)} bytes).")
