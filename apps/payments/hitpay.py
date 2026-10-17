"""
HitPay REST client (API v1).

Requests are form-encoded with the X-BUSINESS-API-KEY header. The
`webhook` request parameter is deprecated by HitPay and is never sent.
"""

import httpx
from django.conf import settings


class HitPayError(Exception):
    """Typed error for non-2xx HitPay responses."""

    def __init__(self, message: str, errors: dict | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or {}
        self.status = status


def _headers() -> dict:
    return {'X-BUSINESS-API-KEY': settings.HITPAY_API_KEY}


def _raise(response: httpx.Response) -> None:
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise HitPayError(
            message=body.get('message', f'HitPay returned {response.status_code}'),
            errors=body.get('errors'),
            status=response.status_code,
        )


def create_payment_request(payload: dict) -> dict:
    """POST /payment-requests — create a HitPay payment request."""
    response = httpx.post(
        f'{settings.HITPAY_API_URL}/payment-requests',
        data=payload,
        headers=_headers(),
        timeout=20,
    )
    _raise(response)
    return response.json()


def get_payment_request(request_id: str) -> dict:
    """GET /payment-requests/{id} — poll a payment request (reconcile)."""
    response = httpx.get(
        f'{settings.HITPAY_API_URL}/payment-requests/{request_id}',
        headers=_headers(),
        timeout=20,
    )
    _raise(response)
    return response.json()


def create_refund(payment_id: str, amount: str) -> dict:
    """POST /refunds — refund a charge by its charge id (NOT the PR id)."""
    response = httpx.post(
        f'{settings.HITPAY_API_URL}/refunds',
        data={'payment_id': payment_id, 'amount': amount},
        headers=_headers(),
        timeout=20,
    )
    _raise(response)
    return response.json()
