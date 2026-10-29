"""
E-Certificate Theme Contract Tests (P4 / spec §7)

theme omitted/None => byte-identical legacy render path.
theme dict at view layer => 400 on non-dict; service drops malformed keys with warnings, never fails.
"""
import io
import json
import logging
from unittest.mock import MagicMock, patch

from PIL import Image

from apps.ecert.services import generate_cert, normalize_theme

ECERT_URL = '/ai/ecert/generate'
WARNING_CAPLOG = logging.WARNING
BASE_PAYLOAD = {
    'runner_name': 'Test Runner',
    'event_name': 'Test Event',
    'bib_number': 'A001',
    'event_date': '2025-01-01',
    'registration_id': 'reg-uuid-1',
}

VALID_THEME = {
    'colors': {
        'primary': '#9C4A1A',
        'secondary': '#3E2C23',
        'accent': '#D4A017',
        'background': '#FBF6EC',
        'foreground': '#2B2118',
    },
    'certificate_background_url': 'https://test.supabase.co/storage/v1/object/public/event-themes/bg.png',
    'labels': {
        'bib': 'Crew ID',
        'checkpoint': 'Kampung',
        'medal': 'Pingat',
        'run': 'Larian',
        'participant': 'Peserta',
        'certificate': 'Sijil',
    },
    'layout': {
        'logo_url': 'https://test.supabase.co/storage/v1/object/public/event-themes/logo.png',
        'badge_icon_url': 'https://test.supabase.co/storage/v1/object/public/event-themes/badge.png',
        'name_plate': 'auto',
    },
}


def _make_sb():
    mock_sb = MagicMock()
    mock_sb.storage.from_.return_value.upload.return_value = MagicMock()
    mock_sb.storage.from_.return_value.create_signed_url.return_value = {
        'signedURL': 'https://test.url/cert.png'
    }
    return mock_sb


class TestNormalizeTheme:
    """Service-side theme-block sanitization contract (spec §7)."""

    def test_none_theme_unchanged(self):
        assert normalize_theme(None) is None

    def test_empty_dict_returns_none(self):
        assert normalize_theme({}) is None

    def test_valid_theme_shape_round_trips(self):
        assert normalize_theme(VALID_THEME) == VALID_THEME

    def test_valid_theme_minimal(self):
        theme = {'colors': {'primary': '#9C4A1A'}}
        assert normalize_theme(theme) == {'colors': {'primary': '#9C4A1A'}}

    def test_unknown_top_level_key_dropped_with_warning(self, caplog):
        with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
            out = normalize_theme({'shady_key': 'x', 'colors': {'primary': '#9C4A1A'}})
        assert out == {'colors': {'primary': '#9C4A1A'}}
        assert any('shady_key' in m for m in caplog.messages)

    def test_unknown_section_keys_dropped_with_warning(self, caplog):
        theme = {
            'colors': {'primary': '#9C4A1A', 'ring': '#C68B2E', 'evil': 1},
            'labels': {'bib': 'Crew ID', 'mystery': 'nope'},
            'layout': {'logo_url': 'https://x/logo.png', 'font_url': None},
        }
        with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
            out = normalize_theme(theme)
        assert out == {
            'colors': {'primary': '#9C4A1A'},
            'labels': {'bib': 'Crew ID'},
            'layout': {'name_plate': 'auto', 'logo_url': 'https://x/logo.png'},
        }
        assert any("'ring'" in m or "'evil'" in m for m in caplog.messages)
        assert any("'mystery'" in m for m in caplog.messages)

    def test_non_hex_color_dropped_with_warning(self, caplog):
        theme = {
            'colors': {
                'primary': '#9C4A1A',
                'secondary': 'red',
                'accent': '#12345',
                'background': 0x68B2E,
                'foreground': True,
            }
        }
        with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
            out = normalize_theme(theme)
        assert out == {'colors': {'primary': '#9C4A1A'}}
        assert len(caplog.records) == 4

    def test_hx_uppercase_hex_accepted(self):
        out = normalize_theme({'colors': {'primary': '#ABCDEF'}})
        assert out == {'colors': {'primary': '#ABCDEF'}}

    def test_label_wrong_type_dropped(self, caplog):
        theme = {'labels': {'bib': 42, 'run': None}}
        with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
            out = normalize_theme(theme)
        assert out is None
        assert len(caplog.records) == 1

    def test_label_over_64_chars_dropped(self, caplog):
        theme = {'labels': {'bib': 'x' * 65}}
        with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
            out = normalize_theme(theme)
        assert out is None
        assert any('64' in m for m in caplog.messages)

    def test_label_suffix_aliases_accepted(self):
        theme = {'labels': {'bib_label': 'Crew ID', 'certificate_label': 'Sijil'}}
        assert normalize_theme(theme) == {'labels': {'bib': 'Crew ID', 'certificate': 'Sijil'}}

    def test_name_plate_non_auto_ignored_keeps_auto(self, caplog):
        theme = {'layout': {'name_plate': 'left', 'logo_url': 'https://x/logo.png'}}
        with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
            out = normalize_theme(theme)
        assert out == {'layout': {'name_plate': 'auto', 'logo_url': 'https://x/logo.png'}}
        assert any('name_plate' in m for m in caplog.messages)

    def test_empty_string_values_treated_as_absent(self):
        theme = {
            'colors': {'primary': ''},
            'labels': {'bib': '   '},
            'certificate_background_url': '',
            'layout': {'logo_url': ''},
        }
        assert normalize_theme(theme) is None

    def test_non_dict_section_value_dropped_not_raised(self, caplog):
        for section in ('colors', 'labels', 'layout'):
            with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
                out = normalize_theme({section: 'red'})
            assert out is None, section
            assert any(section in m for m in caplog.messages)

    def test_non_str_url_value_dropped(self, caplog):
        theme = {'certificate_background_url': 42}
        with caplog.at_level(WARNING_CAPLOG, logger='ecert'):
            out = normalize_theme(theme)
        assert out is None
        assert len(caplog.records) == 1


class TestGenerateCertThemePassThrough:
    """generate_cert(..., theme=...) is additive and backward compatible."""

    def test_legacy_no_theme_returns_signed_url(self):
        sb = _make_sb()
        with patch('apps.ecert.services.get_supabase_client', return_value=sb):
            url = generate_cert('Alice', 'Marathon', 'A001', '2025-01-01', 'reg-1')
        assert url == 'https://test.url/cert.png'

    def test_legacy_render_is_800x600_fallback(self):
        sb = _make_sb()
        with patch('apps.ecert.services.get_supabase_client', return_value=sb):
            generate_cert('Alice', 'Marathon', 'A001', '2025-01-01', 'reg-1')
        uploaded = sb.storage.from_.return_value.upload.call_args.kwargs['file']
        assert Image.open(io.BytesIO(uploaded)).size == (800, 600)

    def test_theme_none_with_explicit_kwarg_is_legacy(self):
        sb = _make_sb()
        with patch('apps.ecert.services.get_supabase_client', return_value=sb):
            generate_cert('Alice', 'Marathon', 'A001', '2025-01-01', 'reg-1', theme=None)
        uploaded = sb.storage.from_.return_value.upload.call_args.kwargs['file']
        assert Image.open(io.BytesIO(uploaded)).size == (800, 600)

    def test_themed_upload_still_certificates_bucket_signed_url(self, monkeypatch):
        # Themed rendering now consumes URLs: stub the SSRF fetcher so this
        # contract test remains network-free (asset errors -> omissible).
        from apps.ecert.assets import ThemeAssetError

        def fail(url, *, max_bytes, **kwargs):
            raise ThemeAssetError(f'stubbed failure: {url}')

        monkeypatch.setattr('apps.ecert.services.fetch_theme_asset', fail)
        sb = _make_sb()
        with patch('apps.ecert.services.get_supabase_client', return_value=sb):
            url = generate_cert(
                'Alice', 'Marathon', 'A001', '2025-01-01', 'reg-1', theme=VALID_THEME
            )
        from_ = sb.storage.from_
        from_.assert_any_call('certificates')
        upload_kwargs = from_.return_value.upload.call_args.kwargs
        assert upload_kwargs['path'] == 'certs/reg-1.png'
        assert upload_kwargs['file_options']['upsert'] == 'true'
        assert url == 'https://test.url/cert.png'

    def test_garbage_theme_dropped_render_continues(self):
        garbage = {'colors': 'red', 'zap': [1, 2], 'labels': 3}
        with patch('apps.ecert.services.get_supabase_client', return_value=_make_sb()):
            url = generate_cert('Alice', 'Marathon', 'A001', '2025-01-01', 'reg-1', theme=garbage)
        assert url == 'https://test.url/cert.png'

    def test_service_never_fails_on_theme(self):
        with patch('apps.ecert.services.get_supabase_client', return_value=_make_sb()):
            for bad in (42, 'red', [], True):
                url = generate_cert('Alice', 'Marathon', 'A001', '2025-01-01', 'reg-1', theme=bad)
                assert url == 'https://test.url/cert.png'


class TestGenerateCertViewTheme:
    """View-level 400 gate + passthrough."""

    def _post(self, api_client, internal_api_key, theme):
        payload = dict(BASE_PAYLOAD)
        payload['theme'] = theme
        return api_client.post(
            ECERT_URL,
            data=json.dumps(payload),
            content_type='application/json',
            **internal_api_key
        )

    def test_theme_int_rejected_400(self, api_client, internal_api_key):
        response = self._post(api_client, internal_api_key, 42)
        assert response.status_code == 400
        assert 'detail' in response.json()

    def test_theme_list_rejected_400(self, api_client, internal_api_key):
        response = self._post(api_client, internal_api_key, [])
        assert response.status_code == 400
        assert 'detail' in response.json()

    def test_theme_str_rejected_400(self, api_client, internal_api_key):
        response = self._post(api_client, internal_api_key, 'Tue')
        assert response.status_code == 400
        assert 'detail' in response.json()

    def test_theme_absent_passes_none(self, api_client, internal_api_key):
        with patch('apps.ecert.views.generate_cert') as mock_gen:
            mock_gen.return_value = 'https://test.url/cert.png'
            payload = dict(BASE_PAYLOAD)
            response = api_client.post(
                ECERT_URL,
                data=json.dumps(payload),
                content_type='application/json',
                **internal_api_key
            )
        assert response.status_code == 200
        assert mock_gen.call_args.kwargs.get('theme') is None

    def test_theme_valid_dict_passed_through(self, api_client, internal_api_key):
        with patch('apps.ecert.views.generate_cert') as mock_gen:
            mock_gen.return_value = 'https://test.url/cert.png'
            response = self._post(api_client, internal_api_key, VALID_THEME)
        assert response.status_code == 200
        assert mock_gen.call_args.kwargs.get('theme') == VALID_THEME

    def test_theme_dict_missing_required_still_400(self, api_client, internal_api_key):
        payload = {'runner_name': 'Test Runner', 'theme': VALID_THEME}
        response = api_client.post(
            ECERT_URL,
            data=json.dumps(payload),
            content_type='application/json',
            **internal_api_key
        )
        assert response.status_code == 400
        assert response.json() == {'error': 'Missing required fields'}
