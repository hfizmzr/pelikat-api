"""
E-Certificate Themed Renderer Tests (P4 Task 3 / spec §7).

Never-fail themed A4-landscape (3508x2480) renderer behind an SSRF-gated
asset fetcher. Every test monkeypatches `apps.ecert.services.fetch_theme_asset`
with in-memory PNG bytes (or error-raising stubs): no test ever hits the
network.
"""
import io
import logging
import struct
import zlib
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from apps.ecert.services import generate_cert, normalize_theme, plate_for

WARNING_LEVEL = logging.WARNING
BG_URL = 'https://test.supabase.co/storage/v1/object/public/event-themes/bg.png'
LOGO_URL = 'https://test.supabase.co/storage/v1/object/public/event-themes/logo.png'
BADGE_URL = 'https://test.supabase.co/storage/v1/object/public/event-themes/badge.png'

# Deliberately odd solid colour so 'background unchanged' probes cannot pass
# by coincidence with plate/text/keyline greys.
SOLID = (171, 205, 239)  # #ABCDEF
CANVAS = (3508, 2480)
MARGIN = 96


def _png(size, color=SOLID, mode='RGB'):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format='PNG')
    return buf.getvalue()


def _header_only_png(size):
    """Minimal valid PNG declaring a huge size (Pillow reads the header only)."""
    width, height = size

    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body))

    ihdr = struct.pack('>II5B', width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    one_row = zlib.compress(b'\x00' + b'\x00' * (width * 3))
    return (
        b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
        + chunk(b'IDAT', one_row) + chunk(b'IEND', b'')
    )


DEFAULT_BG_PNG = _png(CANVAS)
DEFAULT_LOGO_PNG = _png((500, 500))
DEFAULT_BADGE_PNG = _png((300, 300))


def _install_fetch(monkeypatch, *, bg=DEFAULT_BG_PNG, logo=DEFAULT_LOGO_PNG,
                   badge=DEFAULT_BADGE_PNG, error=None):
    """Route fake in-memory PNGs by url suffix; `error` raises for EVERY url."""
    calls = []

    def fake_fetch(url, *, max_bytes):
        calls.append((url, max_bytes))
        if error is not None:
            raise error
        return {'bg.png': bg, 'logo.png': logo, 'badge.png': badge}[url.rsplit('/', 1)[-1]]

    monkeypatch.setattr('apps.ecert.services.fetch_theme_asset', fake_fetch)
    return calls


def _themed(**overrides):
    theme = normalize_theme({
        'colors': {
            'primary': '#9C4A1A',
            'secondary': '#3E2C23',
            'accent': '#D4A017',
            'background': '#FFFFFF',
            'foreground': '#2B2118',
        },
        'certificate_background_url': BG_URL,
        'labels': {
            'bib': 'Crew ID',
            'checkpoint': 'Kampung',
            'medal': 'Pingat',
            'run': 'Larian',
            'participant': 'Peserta',
            'certificate': 'Sijil',
        },
        'layout': {'logo_url': LOGO_URL, 'badge_icon_url': BADGE_URL, 'name_plate': 'auto'},
    })
    theme.update(overrides)
    return theme


def _render(theme=None, name='Alice Tan'):
    """Run generate_cert against a mocked Supabase client; return the PNG image."""
    sb = MagicMock()
    sb.storage.from_.return_value.create_signed_url.return_value = {'signedURL': 's'}
    with patch('apps.ecert.services.get_supabase_client', return_value=sb):
        generate_cert(name, 'Run Kita 2026', 'A001', '2026-11-02', 'reg-t', theme=theme)
    png = sb.storage.from_.return_value.upload.call_args.kwargs['file']
    return Image.open(io.BytesIO(png))


def _rel_lum(rgb):
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb[:3]
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a, b):
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _composite(plate_rgba, fill):
    alpha = plate_rgba[3] / 255
    return tuple(round(alpha * p + (1 - alpha) * f) for p, f in zip(plate_rgba[:3], fill))


class TestPlateFor:
    """plate_for(fill_lux) -> (plate_rgba, text_rgba), >= 7:1 guaranteed pair."""

    def test_light_fill_gets_dark_plate_and_white_text(self):
        plate, text = plate_for(0.95)
        assert sum(plate[:3]) < 90            # strong dark plate
        assert 0.8 <= plate[3] / 255 <= 0.95   # ~85-90% opacity
        assert sum(text[:3]) >= 700            # white text

    def test_dark_fill_gets_white_plate_and_dark_text(self):
        plate, text = plate_for(0.05)
        assert sum(plate[:3]) >= 700
        assert 0.8 <= plate[3] / 255 <= 0.95
        assert sum(text[:3]) <= 90

    @pytest.mark.parametrize('fill', [(255, 255, 255), (18, 18, 18)])
    def test_pair_contrast_at_extremes_ge_7(self, fill):
        plate, text = plate_for(_rel_lum(fill))
        assert _contrast(_composite(plate, fill), text) >= 7.0

    def test_polarity_inverts_between_modes(self):
        dark_plate, white_text = plate_for(0.95)
        light_plate, black_text = plate_for(0.05)
        assert sum(dark_plate[:3]) < sum(light_plate[:3])
        assert sum(white_text[:3]) > sum(black_text[:3])


class TestLegacyUnperturbed:
    """theme omitted / None => byte-identical legacy 800x600 path, zero fetches."""

    def test_no_theme_is_legacy_800x600(self, monkeypatch):
        def explode(url, *, max_bytes, **kwargs):
            raise AssertionError('legacy path must never fetch theme assets')

        monkeypatch.setattr('apps.ecert.services.fetch_theme_asset', explode)
        assert _render().size == (800, 600)

    def test_theme_none_bytes_identical_to_omitted(self):
        with_a = _render(None)
        without = _render()
        buf_a, buf_b = io.BytesIO(), io.BytesIO()
        with_a.save(buf_a, format='PNG')
        without.save(buf_b, format='PNG')
        assert buf_a.getvalue() == buf_b.getvalue()


class TestThemedCanvas:
    """3508x2480 A4 landscape + template gate (>=1600x1130, landscape)."""

    def test_valid_fake_bg_fits_canvas_size(self, monkeypatch):
        _install_fetch(monkeypatch, bg=_png((3200, 2200)))
        assert _render(_themed()).size == CANVAS

    def test_background_capped_at_bg_max_bytes(self, monkeypatch):
        from apps.ecert.assets import MAX_BG_BYTES
        calls = _install_fetch(monkeypatch, bg=_png((3200, 2200)))
        _render(_themed())
        assert (BG_URL, MAX_BG_BYTES) in calls

    def test_bg_exactly_1600x1130_accepted(self, monkeypatch, caplog):
        _install_fetch(monkeypatch, bg=_png((1600, 1130)))
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed())
        assert img.size == CANVAS
        assert not caplog.records

    def test_bg_one_pixel_under_gate_falls_back(self, monkeypatch, caplog):
        _install_fetch(monkeypatch, bg=_png((1599, 1129)))
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed(colors={'background': '#123456'}))
        assert img.size == CANVAS
        assert any('background' in m for m in caplog.messages)
        # fallback canvas is the theme-coloured plain fill (no accent band here)
        assert img.getpixel((5, 500)) == (0x12, 0x34, 0x56)

    def test_bg_portrait_falls_back(self, monkeypatch, caplog):
        _install_fetch(monkeypatch, bg=_png((1130, 1600)))
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed(colors={'background': '#357123'}))
        assert img.size == CANVAS
        assert any('background' in m for m in caplog.messages)
        assert img.getpixel((5, 500)) == (0x35, 0x71, 0x23)

    def test_cap_abort_mid_download_falls_back(self, monkeypatch, caplog):
        from apps.ecert.assets import ThemeAssetError
        _install_fetch(monkeypatch, error=ThemeAssetError('exceeds cap'))
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed(colors={'background': '#241601'}))
        assert img.size == CANVAS
        assert any('exceeds cap' in m for m in caplog.messages)

    def test_bg_over_pixel_ceiling_falls_back(self, monkeypatch, caplog):
        monkeypatch.setattr('PIL.Image.MAX_IMAGE_PIXELS', None)
        _install_fetch(monkeypatch, bg=_header_only_png((20000, 20000)))
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed(colors={'background': '#0F1F2F'}))
        assert img.size == CANVAS
        assert any('pixel ceiling' in m for m in caplog.messages)

    def test_badge_over_pixel_ceiling_omitted(self, monkeypatch, caplog):
        monkeypatch.setattr('PIL.Image.MAX_IMAGE_PIXELS', None)
        _install_fetch(monkeypatch, badge=_header_only_png((20000, 20000)))
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed())
        assert img.size == CANVAS
        assert any('badge' in m and 'pixel ceiling' in m for m in caplog.messages)


class TestOptionalAssets:
    """Logo/badge omissible: fetch failure => omitted + warning, render lives."""

    def test_small_assets_capped_and_both_fetched(self, monkeypatch):
        from apps.ecert.assets import MAX_SMALL_ASSET_BYTES
        calls = _install_fetch(monkeypatch)
        _render(_themed())
        targets = {url for url, _ in calls}
        assert LOGO_URL in targets and BADGE_URL in targets
        assert all(cap == MAX_SMALL_ASSET_BYTES for url, cap in calls if url != BG_URL)

    def test_logo_badge_fetch_error_omitted(self, monkeypatch, caplog):
        from apps.ecert.assets import ThemeAssetError

        def flaky(url, *, max_bytes, **kwargs):
            if url == BG_URL:
                return _png(CANVAS)
            raise ThemeAssetError(f'fetch failed: {url}')

        monkeypatch.setattr('apps.ecert.services.fetch_theme_asset', flaky)
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed())
        assert img.size == CANVAS
        assert any('logo' in m for m in caplog.messages)
        assert any('badge' in m for m in caplog.messages)

    def test_render_safe_catches_everything_to_legacy(self, monkeypatch, caplog):
        _install_fetch(monkeypatch)

        def boom(theme, meta):
            raise RuntimeError('renderer exploded')

        monkeypatch.setattr('apps.ecert.services._render_themed', boom)
        with caplog.at_level(WARNING_LEVEL, logger='ecert'):
            img = _render(_themed())
        assert img.size == (800, 600)
        assert any('fallback' in m for m in caplog.messages)


class TestLayoutContract:
    """Margins, name-block width and char polarity probed on the pixels."""

    def _solid_bg_render(self, monkeypatch, color):
        _install_fetch(monkeypatch, bg=_png(CANVAS, color))
        return _render(_themed(colors={'background': '#ABCDEF', 'foreground': '#2B2118'}))

    def test_everything_inside_96px_inset(self, monkeypatch):
        img = self._solid_bg_render(monkeypatch, SOLID)
        ref = Image.new('RGB', CANVAS, SOLID)
        bbox = ImageChops.difference(img, ref).getbbox()
        assert bbox is not None
        assert bbox[0] >= MARGIN and bbox[1] >= MARGIN
        assert bbox[2] <= CANVAS[0] - MARGIN and bbox[3] <= CANVAS[1] - MARGIN

    def test_name_block_within_60pct_and_margins(self, monkeypatch):
        img = self._solid_bg_render(monkeypatch, SOLID)
        band = img.crop((0, 1130, CANVAS[0], 1230))
        ref = Image.new('RGB', band.size, SOLID)
        bbox = ImageChops.difference(band, ref).getbbox()
        assert bbox is not None  # plate present in this band
        assert bbox[0] >= MARGIN and bbox[2] <= CANVAS[0] - MARGIN
        assert (bbox[2] - bbox[0]) <= int(CANVAS[0] * 0.6)

    def test_plate_polarity_follows_background(self, monkeypatch):
        black = self._solid_bg_render(monkeypatch, (0, 0, 0))
        white = self._solid_bg_render(monkeypatch, (255, 255, 255))

        def plate_pad_mean(img):
            # plate padding column (left of the text bbox), always pure plate
            crop = img.crop((1135, 1160, 1195, 1200))
            return ImageStat.Stat(crop.convert('L')).mean[0]

        assert plate_pad_mean(black) >= 180  # white plate over dark art
        assert plate_pad_mean(white) <= 120  # dark plate over light art

    def test_tall_logo_height_bounded(self, monkeypatch):
        # 200x2000 icon: width would fit the 20% cap, height must not cover rows
        _install_fetch(monkeypatch, logo=_png((200, 2000)))
        img = self._solid_bg_render(monkeypatch, SOLID)
        col = img.crop((190, 0, 420, 1750))  # logo column only, above rows
        ref = Image.new('RGB', col.size, SOLID)
        bbox = ImageChops.difference(col, ref).getbbox()
        assert bbox is not None
        assert bbox[3] <= 660  # heading anchor is 680; asset stays above it

    def test_primary_only_theme_keeps_ink_distinct_from_fill(self, monkeypatch):
        # cr finding: the fallback canvas fill reuses `primary` when no
        # background hex exists — ink must not then equal the fill.
        from apps.ecert.assets import ThemeAssetError
        _install_fetch(monkeypatch, error=ThemeAssetError('stub'))  # no art assets
        img = _render(_themed(colors={'primary': '#F1E8D6'}))
        assert img.size == CANVAS
        assert img.getpixel((5, 500)) == (0xF1, 0xE8, 0xD6)  # canvas from primary
        band = img.crop((0, 1790, CANVAS[0], 2160))  # meta rows: text only band
        ref = Image.new('RGB', band.size, (0xF1, 0xE8, 0xD6))
        assert ImageChops.difference(band, ref).getbbox() is not None  # ink drawn

    def test_degenerate_foreground_flips_to_default_ink(self, monkeypatch):
        # foreground == canvas fill would render invisible; polarity flips ink
        from apps.ecert.assets import ThemeAssetError
        _install_fetch(monkeypatch, error=ThemeAssetError('stub'))
        img = _render(_themed(colors={'foreground': '#FFFFFF', 'background': '#FFFFFF'}))
        assert img.size == CANVAS
        assert img.getpixel((5, 500)) == (255, 255, 255)  # white fallback canvas
        band = img.crop((0, 1790, CANVAS[0], 2160))
        ref = Image.new('RGB', band.size, (255, 255, 255))
        assert ImageChops.difference(band, ref).getbbox() is not None  # ink drawn

    def test_per_field_ink_samples_own_region(self, monkeypatch):
        # two-tone art: dark upper, white lower; rows must NOT be drawn in the
        # ink chosen from the dark plate strip (cr review, T3)
        art = Image.new('RGB', CANVAS, (255, 255, 255))
        ImageDraw.Draw(art).rectangle((0, 0, CANVAS[0], 1300), fill=(0, 0, 0))
        buf = io.BytesIO()
        art.save(buf, format='PNG')
        _install_fetch(monkeypatch, bg=buf.getvalue())
        img = _render(_themed())
        rows_channels_min = [ch[0] for ch in img.crop((210, 1850, 1600, 2200)).getextrema()]
        assert min(rows_channels_min) <= 60  # dark ink drawn on the white lower half
        heading_channels_max = [ch[1] for ch in img.crop((1600, 640, 2400, 860)).getextrema()]
        assert max(heading_channels_max) >= 200  # light ink drawn on the dark upper half

    def test_long_name_truncated_to_name_block(self, monkeypatch):
        _install_fetch(monkeypatch, bg=_png(CANVAS, SOLID))
        img = _render(_themed(colors={'background': '#ABCDEF'}), name='Zul ' * 40)
        band = img.crop((0, 1000, CANVAS[0], 1400))
        ref = Image.new('RGB', band.size, SOLID)
        bbox = ImageChops.difference(band, ref).getbbox()
        assert bbox is not None
        assert (bbox[2] - bbox[0]) <= int(CANVAS[0] * 0.6)
