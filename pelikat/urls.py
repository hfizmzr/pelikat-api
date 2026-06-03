from django.urls import path, include

urlpatterns = [
    path('ai/photos/', include('apps.ai_photos.urls')),
    path('ai/qr/', include('apps.qr_security.urls')),
    path('ai/ecert/', include('apps.ecert.urls')),
    path('ai/badges/', include('apps.badges.urls')),
]