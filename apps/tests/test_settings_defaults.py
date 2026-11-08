"""Settings defaults must be production-safe when the env var is unset (M5)."""
import pytest
from django.core.exceptions import ImproperlyConfigured

import pelikat.settings as settings_module


def _load_fresh_settings(monkeypatch):
    """Execute pelikat/settings.py in a fresh namespace with .env loading
    disabled so the test sees only real environment behaviour."""
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **k: None)
    namespace = {'__file__': settings_module.__file__, '__name__': 'pelikat.settings'}
    with open(settings_module.__file__, encoding='utf-8') as f:
        source = f.read()
    exec(compile(source, 'pelikat/settings.py', 'exec'), namespace)  # noqa: S102
    return namespace


# Smoke: the two most safety-critical settings defaults, boot-fail-closed + DEBUG off.
@pytest.mark.smoke
def test_debug_defaults_false_when_unset(monkeypatch):
    monkeypatch.delenv('DEBUG', raising=False)
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['DEBUG'] is False


# INTERNAL_API_KEY (M-fix): production must refuse to boot without it.
@pytest.mark.smoke
def test_missing_internal_key_raises_when_debug_false(monkeypatch):
    monkeypatch.delenv('INTERNAL_API_KEY', raising=False)
    monkeypatch.delenv('DEBUG', raising=False)
    with pytest.raises(ImproperlyConfigured, match='INTERNAL_API_KEY'):
        _load_fresh_settings(monkeypatch)


def test_missing_internal_key_raises_even_when_debug_true(monkeypatch):
    """Critical #5: fail-closed in ALL modes — empty key could never boot."""
    monkeypatch.delenv('INTERNAL_API_KEY', raising=False)
    monkeypatch.setenv('DEBUG', '1')
    with pytest.raises(ImproperlyConfigured, match='INTERNAL_API_KEY'):
        _load_fresh_settings(monkeypatch)


def test_internal_key_set_ok_when_debug_false(monkeypatch):
    monkeypatch.setenv('INTERNAL_API_KEY', 'secret-key')
    monkeypatch.delenv('DEBUG', raising=False)
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['INTERNAL_API_KEY'] == 'secret-key'
    assert namespace['DEBUG'] is False


def test_debug_enabled_by_explicit_env(monkeypatch):
    monkeypatch.setenv('DEBUG', '1')
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['DEBUG'] is True


# 'api' must survive both the compose default (unset ALLOWED_HOSTS) and a
# Portainer override that omits it — pelikat-web staging calls http://api:8000.
def test_allowed_hosts_always_includes_api_with_env_override(monkeypatch):
    monkeypatch.setenv('ALLOWED_HOSTS', 'staging.example.com')
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['ALLOWED_HOSTS'] == ['staging.example.com', 'api']


def test_allowed_hosts_always_includes_api_when_unset(monkeypatch):
    monkeypatch.delenv('ALLOWED_HOSTS', raising=False)
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['ALLOWED_HOSTS'] == ['localhost', '127.0.0.1', 'api']


# Logging: staging streams root logs to console at an env-driven level —
# default INFO (never DEBUG), DEBUG is an explicit Portainer opt-in.
def test_log_level_defaults_to_info_with_console_handler(monkeypatch):
    monkeypatch.delenv('LOG_LEVEL', raising=False)
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['LOG_LEVEL'] == 'INFO'
    assert namespace['LOGGING']['root'] == {'handlers': ['console'], 'level': 'INFO'}


def test_log_level_env_override_reaches_root_logger(monkeypatch):
    monkeypatch.setenv('LOG_LEVEL', 'debug')
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['LOGGING']['root']['level'] == 'DEBUG'
