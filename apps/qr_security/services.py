"""
QR Security Service

HMAC-SHA256 sign and verify for QR codes.
"""

import hmac
import hashlib
import json
import base64
import time
import secrets
import string

from django.conf import settings


def sign_qr(runner_id: str, event_id: str, bib_number: str) -> str:
    """
    Generate HMAC-SHA256 signed QR payload.

    Args:
        runner_id: UUID of the runner
        event_id: UUID of the event
        bib_number: Bib number assigned to runner

    Returns:
        Base64-encoded signed string: payload_b64.signature
    """
    payload = {
        'runner_id': runner_id,
        'event_id': event_id,
        'bib_number': bib_number,
        'ts': int(time.time()),
    }
    payload_json = json.dumps(payload, separators=(',', ':'))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()

    sig = hmac.new(
        settings.HMAC_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).hexdigest()

    return f"{payload_b64}.{sig}"


def verify_qr(qr_payload: str) -> dict:
    """
    Verify and decode a QR payload.

    Args:
        qr_payload: Base64-encoded signed string

    Returns:
        dict with 'valid' bool and payload data or 'error' message
    """
    try:
        payload_b64, sig = qr_payload.rsplit('.', 1)
        expected_sig = hmac.new(
            settings.HMAC_SECRET.encode(),
            payload_b64.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(sig, expected_sig):
            return {'valid': False, 'error': 'Invalid signature'}

        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())

        return {'valid': True, **payload}
    except Exception as e:
        return {'valid': False, 'error': str(e)}


def generate_consent_code() -> str:
    """
    Generate a 6-character alphanumeric proxy collection code.

    Returns:
        6-char uppercase alphanumeric code, URL-safe
    """
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(6))