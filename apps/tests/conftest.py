import os
import pytest
from django.test import Client, override_settings
from django.conf import settings

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
    
    # Reload settings
    from django.conf import settings as django_settings
    django_settings.INTERNAL_API_KEY = 'test-internal-api-key-12345'
    django_settings.HMAC_SECRET = 'test-hmac-secret-key-12345'
    django_settings.SUPABASE_URL = 'https://test.supabase.co'
    django_settings.SUPABASE_SERVICE_ROLE_KEY = 'test-service-role-key-12345'

@pytest.fixture
def api_client():
    """Django test client fixture."""
    return Client()

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
