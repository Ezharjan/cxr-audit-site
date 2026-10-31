# Fresh PostgreSQL audit database bootstrap fix

Server logs show audit.0001_initial fails with UndefinedTable auth_user. The
router excludes auth tables from the audit database. Although model and 0003
already disable the FK constraint, PostgreSQL fails before reaching 0003.

Correct 0001 to create the user reference without a database constraint. Keep
0003 for existing installations. Do not fake migrations or reset database volumes.
This is within the user's authorized deployment troubleshooting and GSD bypass.

Regression: execute initial migration on isolated SQLite, inspect actual schema
for foreign keys (SQLite otherwise allows missing referenced tables), then apply
remaining migrations and insert a synthetic user ID without an audit auth table.
Actual PostgreSQL retry remains the server-side verification step.
