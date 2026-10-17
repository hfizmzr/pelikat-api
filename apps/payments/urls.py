from django.urls import path

from . import views

urlpatterns = [
    path('create', views.create_payment_view),
    path('webhook/hitpay/', views.hitpay_webhook_view),
    path('status', views.payment_status_view),
    path('refund', views.refund_view),
]
