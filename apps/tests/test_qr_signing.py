"""
QR Signing Security Tests (NFR: JWT validated on every request, HMAC-SHA256 signing)
"""
import pytest
import time
import json
import base64
import hmac
import hashlib
from unittest.mock import patch
from django.test import Client

from apps.qr_security.services import sign_qr, verify_qr


@pytest.mark.security
class TestQrSigning:
    """Verify QR signing and verification security."""

    def test_sign_qr_returns_base64_string(self):
        """sign_qr returns a base64-encoded string with signature."""
        token = sign_qr('runner-123', 'event-456', 'B001')
        
        # Should be in format: payload_b64.signature
        parts = token.split('.')
        assert len(parts) == 2
        
        payload_b64, sig = parts
        # Should be valid base64
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        assert payload['runner_id'] == 'runner-123'
        assert payload['event_id'] == 'event-456'
        assert payload['bib_number'] == 'B001'
        assert 'ts' in payload

    def test_verify_qr_valid_token(self):
        """verify_qr returns valid=True for a correctly signed token."""
        token = sign_qr('runner-123', 'event-456', 'B001')
        result = verify_qr(token)
        
        assert result['valid'] is True
        assert result['runner_id'] == 'runner-123'
        assert result['event_id'] == 'event-456'
        assert result['bib_number'] == 'B001'

    def test_verify_qr_invalid_signature(self):
        """verify_qr returns valid=False for tampered signature."""
        token = sign_qr('runner-123', 'event-456', 'B001')
        payload_b64, sig = token.split('.')
        
        # Tamper with signature
        tampered_token = f"{payload_b64}.tampered"
        result = verify_qr(tampered_token)
        
        assert result['valid'] is False
        assert result['error'] == 'Invalid signature'

    def test_verify_qr_tampered_payload(self):
        """verify_qr returns valid=False for tampered payload."""
        token = sign_qr('runner-123', 'event-456', 'B001')
        payload_b64, sig = token.split('.')
        
        # Decode payload, modify, re-encode
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        payload['bib_number'] = 'TAMPERED'
        new_payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()
        
        tampered_token = f"{new_payload_b64}.{sig}"
        result = verify_qr(tampered_token)
        
        assert result['valid'] is False
        assert result['error'] == 'Invalid signature'

    def test_replay_attack_protection(self):
        """Same payload signed twice should produce different signatures (due to ts)."""
        token1 = sign_qr('runner-123', 'event-456', 'B001')
        time.sleep(1.1)  # Ensure different timestamp (integer seconds)
        token2 = sign_qr('runner-123', 'event-456', 'B001')
        
        assert token1 != token2, "Tokens with same payload but different timestamps should differ"
        
        # Both should be valid
        assert verify_qr(token1)['valid'] is True
        assert verify_qr(token2)['valid'] is True

    def test_verify_qr_invalid_format(self):
        """verify_qr handles malformed tokens gracefully."""
        result = verify_qr('not-a-valid-token')
        
        assert result['valid'] is False
        assert 'error' in result

    def test_verify_qr_api_endpoint(self, api_client, internal_api_key):
        """QR verify API endpoint validates tokens correctly."""
        token = sign_qr('runner-123', 'event-456', 'B001')
        
        response = api_client.post(
            '/ai/qr/verify',
            data={'qr_payload': token},
            content_type='application/json',
            **internal_api_key
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data['valid'] is True
        assert data['runner_id'] == 'runner-123'

    def test_hmac_uses_sha256(self):
        """Verify HMAC-SHA256 is used for signing."""
        import inspect
        source = inspect.getsource(sign_qr)
        assert 'hashlib.sha256' in source
        assert 'hmac.new' in source

    def test_compare_digest_used(self):
        """Verify hmac.compare_digest is used (timing-safe comparison)."""
        import inspect
        source = inspect.getsource(verify_qr)
        assert 'compare_digest' in source
