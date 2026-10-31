"""Exercise audit migration history on isolated in-memory databases.

Run: python deploy/test_audit_bootstrap.py (requires Django).
Checks the initial schema as well as the final schema, so SQLite cannot mask
an invalid reference that PostgreSQL rejects during CREATE TABLE.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "django-app"))

import django
from django.conf import settings

settings.configure(
    SECRET_KEY="isolated-migration-test-only",
    INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "audit"],
    DATABASES={alias: {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
               for alias in ("default", "audit")},
    DATABASE_ROUTERS=["lunit_audit.dbrouters.AuditRouter"],
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    USE_TZ=True,
)
django.setup()

from django.db import connections
from django.db.migrations.executor import MigrationExecutor


class AuditBootstrapTests(unittest.TestCase):
    def test_initial_and_final_schema_do_not_reference_missing_user_table(self):
        connection = connections["audit"]
        executor = MigrationExecutor(connection)
        executor.migrate([("audit", "0001_initial")])
        self.assertNotIn("auth_user", connection.introspection.table_names())
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "audit_auditevent")
        self.assertFalse(any(c.get("foreign_key") for c in constraints.values()),
                         "Initial audit schema must not reference auth_user")

        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        executor.migrate(targets)
        state = executor.loader.project_state(targets)
        event = state.apps.get_model("audit", "AuditEvent")
        record = event.objects.using("audit").create(
            user_id=123, username="synthetic-user", category="UserSelected", action="test")
        self.assertEqual(event.objects.using("audit").get(pk=record.pk).user_id, 123)
        self.assertNotIn("auth_user", connection.introspection.table_names())
        self.assertNotIn("audit_auditevent", connections["default"].introspection.table_names())


if __name__ == "__main__":
    unittest.main()
