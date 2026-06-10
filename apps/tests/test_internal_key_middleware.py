"""
Internal Key Middleware Security Tests (NFR: JWT validated on every API request)
"""
import pytest
from django.test import Client


@pytest.mark.security
class TestInternalKeyMiddleware:
    """Verify that all /ai/* endpoints require X-Internal-Key header."""

    def test_missing_key_returns_401(self, api_client):
        """Request without X-Internal-Key header returns 401."""
        response = api_client.post(
            '/ai/qr/sign',
            data={'runner_id': 'test', 'event_id': 'test', 'bib_number': 'A001'},
            content_type='application/json'
        )
        assert response.status_code == 401
        assert response.json()['error'] == 'Unauthorized'

    def test_wrong_key_returns_401(self, api_client):
        """Request with wrong X-Internal-Key header returns 401."""
        response = api_client.post(
            '/ai/qr/sign',
            data={'runner_id': 'test', 'event_id': 'test', 'bib_number': 'A001'},
            content_type='application/json',
            HTTP_X_INTERNAL_KEY='wrong-key'
        )
        assert response.status_code == 401
        assert response.json()['error'] == 'Unauthorized'

    def test_correct_key_allows_access(self, api_client, internal_api_key):
        """Request with correct X-Internal-Key header passes through."""
        response = api_client.post(
            '/ai/qr/sign',
            data={'runner_id': 'test', 'event_id': 'test', 'bib_number': 'A001'},
            content_type='application/json',
            **internal_api_key
        )
        assert response.status_code == 200

    def test_all_ai_endpoints_protected(self, api_client, internal_api_key):
        """All /ai/* endpoints require the internal key."""
        endpoints = [
            ('/ai/qr/sign', 'POST'),
            ('/ai/qr/verify', 'POST'),
            ('/ai/qr/consent-code', 'POST'),
            ('/ai/ecert/generate', 'POST'),
            ('/ai/badges/evaluate', 'POST'),
            ('/ai/badges/definitions', 'GET'),
        ]
        
        for path, method in endpoints:
            # Without key
            if method == 'POST':
                response = api_client.post(path, data={}, content_type='application/json')
            else:
                response = api_client.get(path)
            assert response.status_code == 401, f"{path} should require auth"
            
            # With key
            if method == 'POST':
                response = api_client.post(path, data={}, content_type='application/json', **internal_api_key)
            else:
                response = api_client.get(path, **internal_api_key)
            assert response.status_code != 401, f"{path} should allow access with valid key"

    def test_non_ai_paths_unprotected(self, api_client):
        """Non-/ai/ paths should not require the internal key."""
        response = api_client.get('/')
        # Should not get 401 from InternalKeyMiddleware
        assert response.status_code != 401
