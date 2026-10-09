#!/bin/bash
set -e

echo "🫁 PRIMER-LLM Django — Starting up..."

# Fix volume permissions (volumes may be created as root)
echo "🔒 Fixing volume permissions..."
chown -R appuser:appuser /app/db /app/media /app/staticfiles /app/audit_artifacts

# Run migrations (as appuser). The audit app is routed to a separate "audit"
# database (AuditRouter), so it must be migrated explicitly with --database=audit;
# a plain migrate only touches the default database.
echo "📦 Running database migrations..."
su -s /bin/bash appuser -c "python manage.py migrate --noinput"
su -s /bin/bash appuser -c "python manage.py migrate --noinput --database=audit"

# Collect static files (WhiteNoise serves them from STATIC_ROOT)
echo "📁 Collecting static files..."
su -s /bin/bash appuser -c "python manage.py collectstatic --noinput"

# Create superuser from env vars if it doesn't exist
if [ -n "$DJANGO_SUPERUSER_USERNAME" ]; then
    echo "👤 Ensuring superuser exists..."
    su -s /bin/bash appuser -c "
        DJANGO_SUPERUSER_USERNAME=$DJANGO_SUPERUSER_USERNAME \
        DJANGO_SUPERUSER_PASSWORD=$DJANGO_SUPERUSER_PASSWORD \
        DJANGO_SUPERUSER_EMAIL=$DJANGO_SUPERUSER_EMAIL \
        python manage.py createsuperuser --noinput" 2>/dev/null || true
fi

# Drop to appuser for Gunicorn
echo "✅ Starting Gunicorn on port 8000..."
exec su -s /bin/bash appuser -c "exec gunicorn lunit_audit.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers ${GUNICORN_WORKERS:-3} \
    --timeout ${GUNICORN_TIMEOUT:-120} \
    --access-logformat '%(h)s %(t)s %(m)s %(U)s %(H)s %(s)s %(b)s' \
    --access-logfile - \
    --error-logfile -"
