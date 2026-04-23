"""
QR Security Views
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import sign_qr, verify_qr


@api_view(['POST'])
def qr_sign(request):
    """
    POST /ai/qr/sign

    Request:
    { "runner_id": "uuid", "event_id": "uuid", "bib_number": "A001" }

    Response:
    { "qr_payload": "base64-encoded-signed-string" }
    """
    data = request.data
    runner_id = data.get('runner_id')
    event_id = data.get('event_id')
    bib_number = data.get('bib_number')

    if not all([runner_id, event_id, bib_number]):
        return Response({'error': 'Missing required fields'}, status=400)

    token = sign_qr(runner_id, event_id, bib_number)
    return Response({'qr_payload': token})


@api_view(['POST'])
def qr_verify(request):
    """
    POST /ai/qr/verify

    Request:
    { "qr_payload": "base64-encoded-signed-string" }

    Response:
    { "valid": true, "runner_id": "uuid", "event_id": "uuid", "bib_number": "A001" }
    """
    qr_payload = request.data.get('qr_payload', '')

    if not qr_payload:
        return Response({'error': 'Missing qr_payload'}, status=400)

    result = verify_qr(qr_payload)
    return Response(result)