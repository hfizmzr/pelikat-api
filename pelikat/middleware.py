"""
Internal API Key Middleware

All /ai/* endpoints require X-Internal-Key header to prevent public access.
"""

from django.conf import settings
from django.http import JsonResponse


class InternalKeyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/ai/'):
            key = request.headers.get('X-Internal-Key', '')
            if key != settings.INTERNAL_API_KEY:
                return JsonResponse({'error': 'Unauthorized'}, status=401)
        return self.get_response(request)