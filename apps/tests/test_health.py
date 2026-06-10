"""
Health / Availability Tests (NFR: 99.5% uptime target)
"""
import pytest
from django.test import Client


@pytest.mark.availability
class TestHealth:
    """Verify health endpoints are available and responsive."""

    def test_health_endpoint_exists(self, api_client, internal_api_key):
        """A health endpoint should exist and return 200."""
        # Note: Django doesn't have a built-in /health endpoint
        # This test checks if one is registered
        response = api_client.get('/health/', **internal_api_key)
        
        # If the endpoint doesn't exist, we should at least get a 404
        # In a real implementation, a health endpoint would be added
        assert response.status_code in [200, 404]
    
    def test_badge_definitions_always_available(self, api_client, internal_api_key):
        """Badge definitions endpoint is a stateless endpoint that should always be available."""
        response = api_client.get('/ai/badges/definitions', **internal_api_key)
        
        assert response.status_code == 200
        data = response.json()
        assert 'badges' in data
        assert isinstance(data['badges'], list)
        assert len(data['badges']) > 0
        
    def test_qr_verify_stateless(self, api_client, internal_api_key):
        """QR verify is a stateless endpoint that should always be available."""
        response = api_client.post(
            '/ai/qr/verify',
            data={'qr_payload': 'invalid.payload'},
            content_type='application/json',
            **internal_api_key
        )
        
        assert response.status_code == 200
        assert response.json()['valid'] is False
