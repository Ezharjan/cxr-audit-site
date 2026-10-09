from django.urls import path
from . import views, config_views

app_name = "agent_api"
urlpatterns = [
    path("settings/api/", config_views.configuration, name="configuration"),
    path("api/<slug:project>/jobs", views.jobs, name="jobs"),
    path("api/<slug:project>/jobs/<uuid:job_id>", views.job_status, name="job_status"),
    path("api/<slug:project>/reports", views.reports, name="reports"),
    path("api/<slug:project>/reports/<slug:report_slug>/pdf", views.pdf, name="pdf"),
    path("api/<slug:project>/reports/<slug:report_slug>/email", views.email, name="email"),
    path("api/<slug:project>/<path:unmatched>", views.not_found, name="not_found"),
]
