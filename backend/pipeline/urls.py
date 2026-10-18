from django.urls import path

from . import views

urlpatterns = [
    path("vocabulary/", views.vocabulary, name="vocabulary"),
    path("vocabulary/<uuid:pk>/", views.vocabulary_detail, name="vocabulary-detail"),
    path("corrections/", views.corrections, name="corrections"),
    path("corrections/<uuid:pk>/", views.correction_detail, name="correction-detail"),
    path("ask/", views.ask_question, name="ask"),
    path("ask/spoken/", views.ask_spoken, name="ask-spoken"),
    path("knowledge/", views.knowledge_list, name="knowledge"),
    path("knowledge/teach/", views.knowledge_teach, name="knowledge-teach"),
    path("knowledge/teach/<uuid:pk>/apply/", views.knowledge_teach_apply, name="knowledge-teach-apply"),
    path("knowledge/<uuid:pk>/", views.knowledge_detail, name="knowledge-detail"),
    path("knowledge/<uuid:pk>/links/<uuid:other>/", views.knowledge_link, name="knowledge-link"),
]
