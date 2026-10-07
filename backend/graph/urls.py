from django.urls import path

from . import views

urlpatterns = [
    path("graph/", views.graph_data, name="graph-data"),
    path("nodes/<uuid:pk>/", views.NodeDetailView.as_view(), name="node-detail"),
]
