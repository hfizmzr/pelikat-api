"""Settings defaults must be production-safe when the env var is unset (M5)."""
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


def test_debug_defaults_false_when_unset(monkeypatch):
    monkeypatch.delenv('DEBUG', raising=False)
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['DEBUG'] is False


def test_debug_enabled_by_explicit_env(monkeypatch):
    monkeypatch.setenv('DEBUG', '1')
    namespace = _load_fresh_settings(monkeypatch)
    assert namespace['DEBUG'] is True
