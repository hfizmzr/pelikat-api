import os

import pytest
from django.test import Client

# Ensure test environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pelikat.settings')

# Dummy test environment
@pytest.fixture(autouse=True, scope='session')
def setup_test_env():
    """Set test environment variables to prevent real API calls."""
    os.environ['INTERNAL_API_KEY'] = 'test-internal-api-key-12345'
    os.environ['HMAC_SECRET'] = 'test-hmac-secret-key-12345'
    os.environ['SUPABASE_URL'] = 'https://test.supabase.co'
    os.environ['SUPABASE_SERVICE_ROLE_KEY'] = 'test-service-role-key-12345'
    os.environ['SECRET_KEY'] = 'test-secret-key-not-for-production'
    os.environ['HITPAY_API_URL'] = 'https://api.sandbox.hit-pay.com/v1'
    os.environ['HITPAY_API_KEY'] = 'test-hitpay-key'
    os.environ['HITPAY_WEBHOOK_SALT'] = 'test-webhook-salt'
    os.environ['HITPAY_PAYMENT_METHODS'] = ''

    # Reload settings
    from django.conf import settings as django_settings
    django_settings.INTERNAL_API_KEY = 'test-internal-api-key-12345'
    django_settings.HMAC_SECRET = 'test-hmac-secret-key-12345'
    django_settings.SUPABASE_URL = 'https://test.supabase.co'
    django_settings.SUPABASE_SERVICE_ROLE_KEY = 'test-service-role-key-12345'
    django_settings.HITPAY_API_URL = 'https://api.sandbox.hit-pay.com/v1'
    django_settings.HITPAY_API_KEY = 'test-hitpay-key'
    django_settings.HITPAY_WEBHOOK_SALT = 'test-webhook-salt'

@pytest.fixture
def api_client():
    """Django test client fixture."""
    return Client()


def pytest_configure(config):
    """Lift the coverage gate for marker-subset runs (e.g. `pytest -m smoke`).

    The repo-wide 90% threshold in pytest.ini addopts is only meaningful for
    full-suite runs (CI); a subset measures ~26% by construction.
    """
    if config.option.markexpr:
        # Mutate the same namespace object the pytest-cov plugin captured
        # (it ignores later config.option changes).
        config.known_args_namespace.cov_fail_under = 0


@pytest.fixture
def internal_api_key():
    """Test internal API key header value."""
    return {'HTTP_X_INTERNAL_KEY': 'test-internal-api-key-12345'}

@pytest.fixture
def mock_supabase():
    """Fixture to mock Supabase client interactions."""
    # This is a simple mock; more sophisticated mocking can be added
    from unittest.mock import MagicMock
    return MagicMock()
