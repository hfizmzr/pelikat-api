from django.urls import path
from . import views

urlpatterns = [
    path('read', views.read_uploaded_photos, name='read_uploaded_photos'),
    path('read-storage', views.read_storage_photos, name='read_storage_photos'),
    path('process', views.process_photos, name='process_photos'),
    path('process-prefix', views.process_photo_prefix, name='process_photo_prefix'),
    path('status/<str:batch_id>', views.photo_status, name='photo_status'),
]
