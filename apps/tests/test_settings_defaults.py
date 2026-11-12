"""Covers: EXT (no census FR) — production-safe settings defaults when env unset.
Layers: settings-module unit tests. Tests: 14.

Settings defaults must be production-safe when the env var is unset (M5)."""
import logging
from pathlib import Path

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


# Incident fallback (staging Portainer stale env): a stack env can still
# carry the legacy heavy-runtime .../best.pt value for the YOLO path;
# settings.py must auto-correct to the bundled models/best.onnx.
_YOLO_ENV = 'AI_PHOTOS_YOLO_MODEL_PATH'


def _yolo_setup(monkeypatch):
    monkeypatch.setenv('INTERNAL_API_KEY', 'ci-dummy-internal-key')
    return _load_fresh_settings(monkeypatch)


def test_yolo_legacy_pt_env_falls_back_to_bundled_onnx_with_warning(monkeypatch, caplog):
    monkeypatch.setenv(_YOLO_ENV, '/app/models/best.pt')
    with caplog.at_level(logging.WARNING, logger='pelikat.settings'):
        namespace = _yolo_setup(monkeypatch)
    expected = str(settings_module.BASE_DIR / 'models' / 'best.onnx')
    assert namespace['AI_PHOTOS_YOLO_MODEL_PATH'] == expected
    assert 'best.pt' in caplog.text and 'best.onnx' in caplog.text


def test_yolo_legacy_pt_env_used_when_onnx_absent(monkeypatch):
    # Hide the bundled ONNX export like the master runtime (best.pt only).
    monkeypatch.setenv(_YOLO_ENV, '/app/models/best.pt')
    real_is_file = Path.is_file

    def no_onnx(self):
        if self.name == 'best.onnx':
            return False
        return real_is_file(self)
    monkeypatch.setattr(Path, 'is_file', no_onnx)
    namespace = _yolo_setup(monkeypatch)
    assert namespace['AI_PHOTOS_YOLO_MODEL_PATH'] == '/app/models/best.pt'


def test_yolo_onnx_env_used_verbatim(monkeypatch):
    monkeypatch.setenv(_YOLO_ENV, '/app/models/best.onnx')
    namespace = _yolo_setup(monkeypatch)
    assert namespace['AI_PHOTOS_YOLO_MODEL_PATH'] == '/app/models/best.onnx'


def test_yolo_custom_pt_not_rewritten_when_onnx_present(monkeypatch):
    monkeypatch.setenv(_YOLO_ENV, '/srv/models/custom-detector.pt')
    namespace = _yolo_setup(monkeypatch)
    assert namespace['AI_PHOTOS_YOLO_MODEL_PATH'] == '/srv/models/custom-detector.pt'


def test_yolo_env_unset_defaults_to_bundled_onnx(monkeypatch):
    monkeypatch.delenv(_YOLO_ENV, raising=False)
    namespace = _yolo_setup(monkeypatch)
    assert namespace['AI_PHOTOS_YOLO_MODEL_PATH'] == str(
        settings_module.BASE_DIR / 'models' / 'best.onnx')
