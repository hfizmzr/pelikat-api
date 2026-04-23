from django.urls import path
from . import views

urlpatterns = [
    path('sign', views.qr_sign, name='qr_sign'),
    path('verify', views.qr_verify, name='qr_verify'),
]