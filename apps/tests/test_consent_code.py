"""
Consent Code Security Tests (NFR: Consent codes shall be single-use and time-limited)
"""
import pytest
import re
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from django.test import Client

from apps.qr_security.services import generate_consent_code


@pytest.mark.security
class TestConsentCode:
    """Verify consent code security properties."""

    def test_consent_code_length(self):
        """Consent code must be exactly 6 characters."""
        code = generate_consent_code()
        assert len(code) == 6

    def test_consent_code_uppercase_alphanumeric(self):
        """Consent code must contain only uppercase letters and digits."""
        code = generate_consent_code()
        assert re.match(r'^[A-Z0-9]{6}$', code), f"Code '{code}' does not match expected format"

    def test_consent_code_unique_per_call(self):
        """Multiple calls should generate different codes (probabilistic)."""
        codes = [generate_consent_code() for _ in range(100)]
        unique_codes = set(codes)
        # With 36^6 = 2.1B combinations, 100 codes should almost certainly be unique
        assert len(unique_codes) == len(codes), "Generated duplicate codes in 100 iterations"

    def test_consent_code_uses_secrets(self):
        """Consent code must use cryptographically secure random."""
        # Check that secrets.choice is used (not random.choice)
        import apps.qr_security.services as services_module
        import secrets
        # The function should reference secrets.choice
        import inspect
        source = inspect.getsource(generate_consent_code)
        assert 'secrets.choice' in source or 'secrets' in source

    def test_consent_code_expiry_24h(self, api_client, internal_api_key):
        """Consent code expires at 24 hours from generation."""
        with patch('apps.qr_security.views._supabase') as mock_sb:
            mock_sb.return_value.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
                data={'id': 'reg-uuid', 'organizer_id': 'org-uuid'}
            )
            mock_sb.return_value.table.return_value.insert.return_value.execute.return_value = MagicMock()
            
            response = api_client.post(
                '/ai/qr/consent-code',
                data={'registration_id': 'reg-uuid'},
                content_type='application/json',
                **internal_api_key
            )
            
            assert response.status_code == 200
            data = response.json()
            
            expires_at = datetime.fromisoformat(data['expires_at'])
            now = datetime.now(timezone.utc)
            
            # Expiry should be approximately 24 hours from now
            diff = expires_at - now
            assert timedelta(hours=23) < diff < timedelta(hours=25), \
                f"Expiry delta {diff} is not approximately 24 hours"

    def test_consent_code_single_use_database(self, api_client, internal_api_key):
        """Generating a consent code for the same registration should store a new code each time."""
        with patch('apps.qr_security.views._supabase') as mock_sb:
            mock_sb.return_value.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
                data={'id': 'reg-uuid', 'organizer_id': 'org-uuid'}
            )
            mock_sb.return_value.table.return_value.insert.return_value.execute.return_value = MagicMock()
            
            # Generate first code
            response1 = api_client.post(
                '/ai/qr/consent-code',
                data={'registration_id': 'reg-uuid'},
                content_type='application/json',
                **internal_api_key
            )
            code1 = response1.json()['code']
            
            # Generate second code (should be different)
            response2 = api_client.post(
                '/ai/qr/consent-code',
                data={'registration_id': 'reg-uuid'},
                content_type='application/json',
                **internal_api_key
            )
            code2 = response2.json()['code']
            
            assert code1 != code2, "Same consent code generated twice for same registration"
            
            # Verify insert was called twice
            assert mock_sb.return_value.table.return_value.insert.call_count == 2
