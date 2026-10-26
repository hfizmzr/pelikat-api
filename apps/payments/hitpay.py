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


def list_payment_requests(params: dict) -> list:
    """GET /v1/payment-requests — filterable PR list, used only by sentinel
    recovery. Backend-supported filters (docs.hitpayapp.com,
    /apis/payment-request/get-payment-requests): `search` (partial match on
    email, id, or reference_number) and `metadata` (exact key/value match,
    first pair wins — sent as `metadata[key]` form params). Any 4xx/5xx
    raises HitPayError; a 200 with an empty/absent collection is a valid
    "no candidates" success."""
    response = httpx.get(
        f'{settings.HITPAY_API_URL}/payment-requests',
        params=params,
        headers=_headers(),
        timeout=20,
    )
    _raise(response)
    body = response.json()
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ('data', 'payment_requests'):
            value = body.get(key)
            if isinstance(value, list):
                return value
    return []


def delete_payment_request(request_id: str) -> dict | None:
    """DELETE /payment-requests/{id} — cancel an incomplete payment request.

    Used when a locally-released stale checkout's remote PR must be voided
    so its checkout URL cannot still be paid. The endpoint is published in
    HitPay's v1 OpenAPI but undocumented in the classic docs; HitPay
    answers 4xx (e.g. 404 unknown / 409 already-completed) for PRs it will
    not void — callers treat ANY error as "leave it to the webhook" and
    must not fail the surrounding flow. Returns the JSON body on 2xx, or
    None on a 4xx/5xx (non-raising: `_raise` failure is swallowed).
    """
    response = httpx.delete(
        f'{settings.HITPAY_API_URL}/payment-requests/{request_id}',
        headers=_headers(),
        timeout=20,
    )
    try:
        _raise(response)
    except HitPayError:
        return None
    return response.json() if response.content else None


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
