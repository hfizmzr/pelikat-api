from django.urls import path
from . import views

urlpatterns = [
    path('process', views.process_photos, name='process_photos'),
    path('status/<str:batch_id>', views.photo_status, name='photo_status'),
]