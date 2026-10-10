"""Synthetic verification only: never connects to deployment databases or sends SMTP."""
from lunit_audit.settings import *

DEBUG = True
SECRET_KEY = "synthetic-agent-api-test-secret"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SECURE_SSL_REDIRECT = False
LLM_ALLOW_INSECURE_TRANSPORT = True
DATABASES = {alias: {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"} for alias in ("default", "audit")}
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DEFAULT_FROM_EMAIL = "primer@example.invalid"

CXR_API_CONFIG = {"API_IP": "127.0.0.1", "API_PORT": 9}
