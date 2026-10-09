import uuid

from django.conf import settings
from django.db import models


class APIConfiguration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    changed_at = models.DateTimeField(auto_now=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="api_configuration_singleton")]


class APIKey(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=100)
    public_id = models.CharField(max_length=32, unique=True)
    secret_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)


class APIKeyProject(models.Model):
    key = models.ForeignKey(APIKey, related_name="projects", on_delete=models.CASCADE)
    project_id = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "project_id"], name="api_key_project_unique")]


class APIJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.OneToOneField("upload.ProcessingTask", on_delete=models.PROTECT)
    project_id = models.CharField(max_length=64)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    creator_key = models.ForeignKey(APIKey, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class IdempotencyRecord(models.Model):
    key = models.ForeignKey(APIKey, on_delete=models.PROTECT)
    project_id = models.CharField(max_length=64)
    operation = models.CharField(max_length=150)
    value = models.CharField(max_length=128)
    request_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=20, default="in_progress")
    response = models.JSONField(default=dict)
    status_code = models.PositiveSmallIntegerField(default=409)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "project_id", "operation", "value"], name="api_idempotency_unique")]


class PDFReservation(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    token = models.UUIDField(null=True)
    expires_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="api_pdf_singleton")]
