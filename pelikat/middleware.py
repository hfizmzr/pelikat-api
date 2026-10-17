"""
Internal API Key Middleware

All /ai/* and /payments/* endpoints require X-Internal-Key header to
prevent public access — except the HitPay webhook path, which is
protected by Hitpay-Signature verification instead.
"""

from django.conf import settings
from django.http import JsonResponse

_WEBHOOK_EXEMPT_PREFIX = '/payments/webhook/'


def _requires_internal_key(path: str) -> bool:
    if path.startswith('/ai/'):
        return True
    if path.startswith('/payments/'):
        return not path.startswith(_WEBHOOK_EXEMPT_PREFIX)
    return False


class InternalKeyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _requires_internal_key(request.path):
            key = request.headers.get('X-Internal-Key', '')
            if key != settings.INTERNAL_API_KEY:
                return JsonResponse({'error': 'Unauthorized'}, status=401)
        return self.get_response(request)