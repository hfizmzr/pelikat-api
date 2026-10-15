"""
QR Security Views
"""

from datetime import datetime, timedelta, timezone

from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.response import Response
from supabase import create_client

from .services import sign_qr, verify_qr, generate_consent_code


def _supabase():
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


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


@api_view(['POST'])
def consent_code(request):
    """
    POST /ai/qr/consent-code

    Generate a consent code for proxy race pack collection.

    Request:
    { "registration_id": "uuid" }

    Response:
    { "code": "A1B2C3", "expires_at": "2025-06-03T12:00:00Z" }
    """
    registration_id = request.data.get('registration_id')
    if not registration_id:
        return Response({'error': 'Missing registration_id'}, status=400)

    sb = _supabase()

    result = (
        sb.table('registrations')
        .select('id, organizer_id')
        .eq('id', registration_id)
        .single()
        .execute()
    )

    if not result.data:
        return Response({'error': 'Registration not found'}, status=404)

    reg = result.data
    code = generate_consent_code()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    sb.table('repc_consent_codes').update({'is_used': True})\
        .eq('registration_id', registration_id)\
        .eq('is_used', False)\
        .execute()

    sb.table('repc_consent_codes').insert({
        'registration_id': registration_id,
        'organizer_id': reg['organizer_id'],
        'code': code,
        'expires_at': expires_at.isoformat(),
    }).execute()

    return Response({'code': code, 'expires_at': expires_at.isoformat()})