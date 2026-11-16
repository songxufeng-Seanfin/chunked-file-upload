from django.urls import path

from . import views

urlpatterns = [
    path("", views.console, name="console"),
    path("api/state/", views.api_state, name="api_state"),
    path("api/upload/check/", views.api_check, name="api_check"),
    path("api/upload/chunk/", views.api_chunk, name="api_chunk"),
    path("api/upload/merge/", views.api_merge, name="api_merge"),
    path("api/demo/run/", views.api_demo, name="api_demo"),
    path("api/demo/reset/", views.api_reset, name="api_reset"),
]
