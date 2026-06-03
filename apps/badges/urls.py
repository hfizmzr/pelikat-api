from django.urls import path
from . import views

urlpatterns = [
    path("evaluate", views.evaluate_badges_view, name="evaluate_badges"),
    path("definitions", views.badge_definitions_view, name="badge_definitions"),
]
