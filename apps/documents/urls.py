from django.urls import path
from . import views

urlpatterns = [
    path("encrypt", views.encrypt_view, name="documents_encrypt"),
    path("encrypt-ic", views.encrypt_ic_view, name="documents_encrypt_ic"),
    path("decrypt", views.decrypt_view, name="documents_decrypt"),
    path("delete",  views.delete_view,  name="documents_delete"),
]
