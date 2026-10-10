# PRIMER — LLM-based Chest X-Ray Audit Tool
# Copyright (C) 2026 Goh Shu Wen
# Licensed under AGPL-3.0-or-later. See LICENSE at the repository root.
"""
URL configuration for the upload app.
"""

from django.urls import path
from . import views, workflow_views, config_views

app_name = 'upload'

urlpatterns = [
    path('configuration/', config_views.configuration, name='configuration'),
    # Main interface
    path('', views.index, name='index'),
    
    # Tasks list
    path('tasks/', views.tasks_list, name='tasks_list'),
    path('api/tasks/<str:task_id>/delete', views.delete_task, name='delete_task'),
    path('api/tasks/<str:task_id>/cancel', views.cancel_task, name='cancel_task'),
    path('api/backfill-supplemental', views.backfill_supplemental, name='backfill_supplemental'),
    
    # Import historical data
    path('import/', workflow_views.retired_import, name='import_data'),
    path('api/import/preview', workflow_views.retired_import, name='import_preview'),
    path('api/import/confirm', workflow_views.retired_import, name='import_confirm'),
    
    # API server health check
    path('api/check-connection', views.check_api_connection, name='check_api_connection'),

    # LLM server health check
    path('api/check-llm', views.check_llm_connection, name='check_llm_connection'),
    
    # Pre-check files against database
    path('api/precheck', workflow_views.precheck, name='precheck_files'),
    
    # Active task check (for resume-on-page-load)
    path('api/active-task', views.get_active_task, name='get_active_task'),
    
    # Proxy endpoints to backend API server
    path('api/analyze-multiple', workflow_views.submit, name='analyze_multiple'),
    path('api/analyze-auto-sort', workflow_views.submit, name='analyze_auto_sort'),
    path('api/status/<str:task_id>', views.get_status, name='get_status'),
    path('api/results/<str:task_id>', views.get_results, name='get_results'),
    
    # Saved task results from background worker
    path('api/task-results/<str:task_id>', views.get_task_results, name='get_task_results'),
]
