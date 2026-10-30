"""
Payments Views
"""


from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import services


@api_view(['POST'])
def create_payment_view(request):
    """
    POST /payments/create  (internal-key)

    Body: {order_type, registration_id?, merch_order_id?, organizer_id?,
           runner_id?, amount, currency?, payment_methods?, email, name,
           phone?, purpose, redirect_url}
    """
    required = ('order_type', 'amount', 'email', 'name')
    missing = [f for f in required if not request.data.get(f)]
    if missing:
        return Response({'error': f'Missing fields: {", ".join(missing)}'}, status=400)
    if request.data.get('order_type') == 'registration' \
            and not request.data.get('registration_id'):
        return Response({'error': 'registration_id is required'}, status=400)

    status, body = services.create_payment(request.data)
    return Response(body, status=status)


@api_view(['POST'])
def hitpay_webhook_view(request):
    """
    POST /payments/webhook/hitpay/  (signature-protected, NOT internal-key)

    Verifies Hitpay-Signature (HMAC-SHA256 hex of the raw body keyed by
    HITPAY_WEBHOOK_SALT). Fails closed with 403.
    """
    raw_body = request.body
    signature = request.headers.get('Hitpay-Signature', '')
    event_type = request.headers.get('Hitpay-Event-Type', '')

    status, body = services.handle_webhook(raw_body, signature, event_type)
    return Response(body, status=status)


@api_view(['GET'])
def payment_status_view(request):
    """
    GET /payments/status?reference_number=<registration_id>  (internal-key)
    """
    reference_number = request.query_params.get('reference_number')
    if not reference_number:
        return Response({'error': 'reference_number is required'}, status=400)
    status, body = services.get_payment_status(reference_number)
    return Response(body, status=status)


@api_view(['POST'])
def refund_view(request):
    """
    POST /payments/refund  (internal-key)

    Body: {provider_request_id, amount?}
    """
    provider_request_id = request.data.get('provider_request_id')
    if not provider_request_id:
        return Response({'error': 'provider_request_id is required'}, status=400)
    status, body = services.refund_payment(
        provider_request_id, request.data.get('amount')
    )
    return Response(body, status=status)
