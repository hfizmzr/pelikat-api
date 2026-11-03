"""
Badge Evaluate Input-Validation Tests (regression: 22P02 -> 500)

run_logs.runner_id / runner_badges.runner_id are UUID columns in Supabase.
PostgREST rejects a non-UUID runner_id with 22P02 ("invalid input syntax
for type uuid"), which escaped _gather_stats as an uncaught APIError and
surfaced as a 500 "internal error" (seen with k6 placeholder ids like
'k6-runner'). The view must reject non-UUID ids up-front with 400.
"""
from unittest.mock import patch

import pytest

UUID = '00000000-0000-0000-0000-000000000001'


@pytest.mark.reliability
class TestBadgeEvaluateValidation:
    """POST /ai/badges/evaluate must validate runner_id is a UUID."""

    def test_non_uuid_runner_id_returns_400(self, api_client, internal_api_key):
        """A malformed runner_id is a client error (400), not a 500."""
        response = api_client.post(
            '/ai/badges/evaluate',
            data={'runner_id': 'k6-runner'},
            content_type='application/json',
            **internal_api_key
        )
        assert response.status_code == 400, response.json()
        assert 'runner_id' in response.json().get('error', '')

    def test_non_uuid_string_variants_return_400(self, api_client, internal_api_key):
        """Blank, empty and malformed ids must all be rejected."""
        for bad in ['', '   ', 'test-runner', '12345', None]:
            response = api_client.post(
                '/ai/badges/evaluate',
                data={'runner_id': bad},
                content_type='application/json',
                **internal_api_key
            )
            assert response.status_code == 400, f"runner_id={bad!r} -> {response.status_code}"

    def test_valid_uuid_runner_id_reaches_service(self, api_client, internal_api_key):
        """A valid UUID is not rejected and is passed through to the service."""
        with patch('apps.badges.views.evaluate_badges') as mock_evaluate:
            mock_evaluate.return_value = {'awarded': []}
            response = api_client.post(
                '/ai/badges/evaluate',
                data={'runner_id': UUID, 'event_id': None},
                content_type='application/json',
                **internal_api_key
            )
            assert response.status_code == 200
            args = mock_evaluate.call_args[0]
            assert args[0] == UUID

    def test_valid_uuid_with_event_id_reaches_service(self, api_client, internal_api_key):
        """event_id (when present) must also be a valid UUID."""
        with patch('apps.badges.views.evaluate_badges') as mock_evaluate:
            mock_evaluate.return_value = {'awarded': []}
            response = api_client.post(
                '/ai/badges/evaluate',
                data={'runner_id': UUID, 'event_id': 'not-a-uuid'},
                content_type='application/json',
                **internal_api_key
            )
            assert response.status_code == 400
            assert 'event_id' in response.json().get('error', '')

            mock_evaluate.reset_mock()
            response = api_client.post(
                '/ai/badges/evaluate',
                data={'runner_id': UUID, 'event_id': '00000000-0000-0000-0000-000000000abc'},
                content_type='application/json',
                **internal_api_key
            )
            assert response.status_code == 200
            args = mock_evaluate.call_args[0]
            assert args[1] == '00000000-0000-0000-0000-000000000abc'
