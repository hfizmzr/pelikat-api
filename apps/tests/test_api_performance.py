"""
API Performance Tests (NFR: CRUD responses < 500ms under normal load)
"""
import time
import pytest
from django.test import Client
from unittest.mock import patch, MagicMock

@pytest.mark.performance
class TestApiPerformance:
    """Verify that all API endpoints respond within 500ms."""

    def test_qr_sign_response_time(self, api_client, internal_api_key):
        """POST /ai/qr/sign shall complete within 500ms."""
        with patch('apps.qr_security.views._supabase') as mock_sb:
            mock_sb.return_value.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(data=None)
            
            start = time.perf_counter()
            response = api_client.post(
                '/ai/qr/sign',
                data={'runner_id': 'test-uuid', 'event_id': 'test-uuid', 'bib_number': 'A001'},
                content_type='application/json',
                **internal_api_key
            )
            elapsed = time.perf_counter() - start
            
            assert response.status_code == 200
            assert elapsed < 0.5, f"QR sign took {elapsed:.3f}s, exceeds 500ms"

    def test_qr_verify_response_time(self, api_client, internal_api_key):
        """POST /ai/qr/verify shall complete within 500ms."""
        # First sign a QR
        with patch('apps.qr_security.views._supabase'):
            sign_response = api_client.post(
                '/ai/qr/sign',
                data={'runner_id': 'test-uuid', 'event_id': 'test-uuid', 'bib_number': 'A001'},
                content_type='application/json',
                **internal_api_key
            )
            qr_payload = sign_response.json()['qr_payload']
        
        start = time.perf_counter()
        response = api_client.post(
            '/ai/qr/verify',
            data={'qr_payload': qr_payload},
            content_type='application/json',
            **internal_api_key
        )
        elapsed = time.perf_counter() - start
        
        assert response.status_code == 200
        assert response.json()['valid'] is True
        assert elapsed < 0.5, f"QR verify took {elapsed:.3f}s, exceeds 500ms"

    def test_consent_code_response_time(self, api_client, internal_api_key):
        """POST /ai/qr/consent-code shall complete within 500ms."""
        with patch('apps.qr_security.views._supabase') as mock_sb:
            mock_sb.return_value.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
                data={'id': 'reg-uuid', 'organizer_id': 'org-uuid'}
            )
            mock_sb.return_value.table.return_value.insert.return_value.execute.return_value = MagicMock()
            
            start = time.perf_counter()
            response = api_client.post(
                '/ai/qr/consent-code',
                data={'registration_id': 'reg-uuid'},
                content_type='application/json',
                **internal_api_key
            )
            elapsed = time.perf_counter() - start
            
            assert response.status_code == 200
            assert 'code' in response.json()
            assert elapsed < 0.5, f"Consent code took {elapsed:.3f}s, exceeds 500ms"

    def test_badge_definitions_response_time(self, api_client, internal_api_key):
        """GET /ai/badges/definitions shall complete within 500ms."""
        start = time.perf_counter()
        response = api_client.get(
            '/ai/badges/definitions',
            **internal_api_key
        )
        elapsed = time.perf_counter() - start
        
        assert response.status_code == 200
        data = response.json()
        assert 'badges' in data
        assert isinstance(data['badges'], list)
        assert elapsed < 0.5, f"Badge definitions took {elapsed:.3f}s, exceeds 500ms"

    def test_badge_evaluate_response_time(self, api_client, internal_api_key):
        """POST /ai/badges/evaluate shall complete within 500ms."""
        with patch('apps.badges.views.evaluate_badges') as mock_evaluate:
            mock_evaluate.return_value = {'awarded': [{'badge_key': 'first_run', 'name': 'First Run', 'description': 'Test', 'icon': '🏃'}]}
            
            start = time.perf_counter()
            response = api_client.post(
                '/ai/badges/evaluate',
                data={'runner_id': 'runner-uuid'},
                content_type='application/json',
                **internal_api_key
            )
            elapsed = time.perf_counter() - start
            
            assert response.status_code == 200
            assert 'awarded' in response.json()
            assert elapsed < 0.5, f"Badge evaluate took {elapsed:.3f}s, exceeds 500ms"

    def test_ecert_generate_response_time(self, api_client, internal_api_key):
        """POST /ai/ecert/generate shall complete within 500ms."""
        with patch('apps.ecert.views.generate_cert') as mock_gen:
            mock_gen.return_value = 'https://test.supabase.co/storage/v1/object/sign/certificates/test.png'
            
            start = time.perf_counter()
            response = api_client.post(
                '/ai/ecert/generate',
                data={
                    'runner_name': 'Test Runner',
                    'event_name': 'Test Event',
                    'bib_number': 'A001',
                    'event_date': '2025-01-01',
                    'registration_id': 'reg-uuid'
                },
                content_type='application/json',
                **internal_api_key
            )
            elapsed = time.perf_counter() - start
            
            assert response.status_code == 200
            assert elapsed < 0.5, f"E-cert generation took {elapsed:.3f}s, exceeds 500ms"
