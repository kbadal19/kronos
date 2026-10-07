from django.urls import path

from . import views

urlpatterns = [
    path("captures/", views.upload_capture, name="capture-upload"),
    path("captures/list/", views.CaptureListView.as_view(), name="capture-list"),
    path("captures/health/", views.capture_health, name="capture-health"),
    path("captures/<uuid:pk>/", views.CaptureDetailView.as_view(), name="capture-detail"),
    path("captures/<uuid:pk>/transcript/", views.edit_transcript, name="capture-edit"),
    path("captures/<uuid:pk>/renormalize/", views.renormalize_capture, name="capture-renormalize"),
]
