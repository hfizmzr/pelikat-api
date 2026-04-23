from django.urls import path
from . import views

urlpatterns = [
    path('generate', views.generate_cert_view, name='generate_cert'),
]