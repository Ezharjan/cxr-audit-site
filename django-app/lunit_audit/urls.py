# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""
URL configuration for lunit_audit project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect
from django.views.generic.base import RedirectView

urlpatterns = [
    path("", include("agent_api.urls")),
    path("", lambda request: redirect("upload:index")),
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(http_method_names=["post"]), name="logout"),
    path("upload/", include("upload.urls")),
    path("view/", include("viewer.urls")),
    path("report-old/", include("report.urls")),
    path("report/", include("report_v2.urls")),
    path("editor/", include("report_v2.editor_urls")),
    path("layout/", RedirectView.as_view(pattern_name="report_editor:editor", permanent=False)),
    path("layout/", include("report_v2.legacy_editor_urls")),
    path("gt/", include("gt.urls")),
]
