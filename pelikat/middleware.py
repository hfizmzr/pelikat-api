"""
Internal API Key Middleware

All /ai/* and /payments/* endpoints require X-Internal-Key header to
prevent public access — except the HitPay webhook path, which is
protected by Hitpay-Signature verification instead.
"""

import hmac

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
            # L1: constant-time comparison (timing-attack safe). Fail-closed:
            # empty/missing header always rejected, no DEBUG fallback.
            # Compare as bytes: WSGI headers are latin-1 decoded, so a
            # non-ASCII X-Internal-Key would make compare_digest(str, str)
            # raise TypeError (→ 500) instead of failing as 401.
            if not key or not hmac.compare_digest(
                    key.encode('utf-8'),
                    str(settings.INTERNAL_API_KEY).encode('utf-8')):
                return JsonResponse({'error': 'Unauthorized'}, status=401)
        return self.get_response(request)