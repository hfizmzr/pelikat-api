"""
E-Certificate Generation Service

Uses Pillow to generate PNG e-certificates and upload to Supabase Storage.
"""

import io
import logging
import os
import re
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps

from pelikat.supabase import get_sb as get_supabase_client

from apps.ecert.assets import (
    MAX_BG_BYTES,
    MAX_SMALL_ASSET_BYTES,
    ThemeAssetError,
    fetch_theme_asset,
)

_log = logging.getLogger('ecert')

_HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}$')

_COLOR_KEYS = ('primary', 'secondary', 'accent', 'background', 'foreground')
_LABEL_KEYS = ('bib', 'checkpoint', 'medal', 'run', 'participant', 'certificate')
_LAYOUT_KEYS = ('logo_url', 'badge_icon_url', 'name_plate')


def _clean_str(section, key, value) -> str | None:
    """Return a stripped string, or None if absent/empty/malformed (warned)."""
    if value is None:
        return None  # absent
    if not isinstance(value, str):
        _log.warning('theme %s: dropped malformed value for %r (%s)', section, key, type(value).__name__)
        return None
    stripped = value.strip()
    if stripped == '':
        return None  # empty strings are rejected as absent
    return stripped


def _clean_hex(section, key, value) -> str | None:
    s = _clean_str(section, key, value)
    if s is None:
        return None
    if not _HEX_RE.match(s):
        _log.warning('theme %s: dropped non-HEX value for %r (%s)', section, key, type(value).__name__)
        return None
    return s


def _clean_section(value, key, section_keys, clean_value):
    """Sanitize a nested dict section (colors/labels/layout); drop unknown keys with warnings."""
    if value is None:
        return None
    if not isinstance(value, dict):
        _log.warning('theme: dropped %r (expected object, got %s)', key, type(value).__name__)
        return None
    cleaned = {}
    for sk, sv in value.items():
        if sk not in section_keys:
            _log.warning('theme %s: dropped unknown key %r', key, sk)
            continue
        if key == 'layout' and sk == 'name_plate':
            if sv != 'auto':
                # Log the key only: the value is user-controlled payload and
                # must never reach the log line.
                _log.warning(
                    'theme layout: name_plate ignored '
                    '(layout key sent non-auto value; kept auto)'
                )
            continue
        cleaned_value = clean_value(key, sk, sv)
        if cleaned_value is None:
            continue
        if key == 'labels' and len(cleaned_value) > 64:
            _log.warning('theme labels: dropped %r (over 64 chars)', sk)
            continue
        cleaned[sk] = cleaned_value
    if key == 'layout' and cleaned:
        cleaned['name_plate'] = 'auto'
    return cleaned or None


def normalize_theme(theme: dict | None) -> dict | None:
    """
    Sanitize the optional e-cert theme block (spec §7).

    Unknown keys and malformed values are dropped with a warning on the
    'ecert' logger; this function NEVER raises and NEVER fails a request.
    Returns None when no valid theme content remains (legacy path).
    """
    if theme is None:
        return None
    if not isinstance(theme, dict):
        _log.warning('theme: non-dict theme ignored (view should have rejected this)')
        return None

    out = {}
    for key, value in theme.items():
        if key == 'colors':
            colors = _clean_section(value, key, _COLOR_KEYS, _clean_hex)
            if colors:
                out['colors'] = colors
        elif key == 'labels':
            labels = _clean_section(value, key, _LABEL_KEYS, _clean_str)
            if labels:
                out['labels'] = labels
        elif key == 'layout':
            layout = _clean_section(value, key, _LAYOUT_KEYS, _clean_str)
            if layout:
                out['layout'] = layout
        elif key == 'certificate_background_url':
            url = _clean_str('certificate_background_url', key, value)
            if url is not None:
                out['certificate_background_url'] = url
        else:
            _log.warning('theme: dropped unknown key %r', key)

    return out or None


def generate_cert(
    runner_name: str,
    event_name: str,
    bib_number: str,
    event_date: str,
    registration_id: str,
    theme: dict | None = None
) -> str:
    """
    Generate PNG e-cert, upload to Supabase Storage, return signed URL.

    Args:
        runner_name: Full name of the runner
        event_name: Name of the event
        bib_number: Bib number assigned
        event_date: Date of the event (string)
        registration_id: UUID of the registration
        theme: Optional sanitized theme block (dict | None). None/absent
            keeps the legacy render path byte-identical.

    Returns:
        Signed URL (24h expiry)
    """
    theme = normalize_theme(theme)
    sb = get_supabase_client()

    img = _render_cert(runner_name, event_name, bib_number, event_date, theme)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)

    storage_path = f"certs/{registration_id}.png"
    sb.storage.from_('certificates').upload(
        path=storage_path,
        file=buf.read(),
        file_options={'content-type': 'image/png', 'upsert': 'true'}
    )

    res = sb.storage.from_('certificates').create_signed_url(storage_path, 86400)
    return res['signedURL']

def _render_cert(runner_name, event_name, bib_number, event_date, theme=None):
    """Dispatch rendering: themed (never fails) or the verbatim legacy seam."""
    if theme:
        return _render_safe(theme, {
            'runner_name': runner_name,
            'event_name': event_name,
            'bib_number': bib_number,
            'event_date': event_date,
        })
    return _render_legacy(runner_name, event_name, bib_number, event_date)


# --- Themed A4-landscape renderer (spec §7) ---------------------------------

_CANVAS_SIZE = (3508, 2480)   # A4 landscape @ 300 DPI
_MIN_BG_SIZE = (1600, 1130)   # template gate: min size, landscape
_SAFE_MARGIN = 96             # 8 mm safe margins
_PLATE_MAX_FRAC = 0.6         # name block <= 60% canvas width
_LOGO_MAX_FRAC = 0.2
_BADGE_MAX_FRAC = 0.12
_LIGHT_FILL_LUX = 0.18         # linear-luminance crossover (white/black ink 1:1)
_FONT_DIR = '/usr/share/fonts/truetype/dejavu/'

# Plate polarity pairs: each (plate_rgba, text_rgba) pair composites to a
# >= 7:1 WCAG contrast for ANY underlying fill (verified at both extremes in
# test_ecert_render.py), so the constant pairs are safe for all inputs.
_DARK_PLATE = ((26, 26, 26, 217), (255, 255, 255, 255))    # ~85% + white
_LIGHT_PLATE = ((255, 255, 255, 235), (17, 17, 17, 255))   # ~92% + near-black

# Layout anchors (all insets >= _SAFE_MARGIN; probed in test_ecert_render.py).
# Every text field is centered on its own auto-polarity backing plate.
_TEXT_INSET_X = 200
_PLATE_CENTER = (_CANVAS_SIZE[0] // 2, 1180)
_HEADING_CENTER = (_CANVAS_SIZE[0] // 2, 700)
_BIB_CENTER = (_CANVAS_SIZE[0] // 2, 1580)
_ROWS_TOP = 1800
_ROW_STEP = 140
_PLATE_PAD = (80, 56)        # name plate padding
_SMALL_PLATE_PAD = (16, 8)   # heading/bib/row padding
_ASSET_MAX_H = 400          # keeps thumbnails above the heading plate
_MAX_ASSET_PIXELS = 35_000_000   # ~4x the certificate canvas
_MAX_ASSET_DIM = 12_000
_RENDER_DEADLINE_SECONDS = 90.0  # gunicorn --timeout 120 leaves render slack

# Per-field fill-sample regions (each text field is inked against the art it
# lands on, not against the name-plate strip — cr review, T3).
_PLATE_FILL_BOX = (
    int(_CANVAS_SIZE[0] * 0.2), _PLATE_CENTER[1] - 40,
    int(_CANVAS_SIZE[0] * 0.8), _PLATE_CENTER[1] + 40,
)
_HEADING_FILL_BOX = (
    int(_CANVAS_SIZE[0] * 0.2), 600, int(_CANVAS_SIZE[0] * 0.8), 900
)
_BIB_FILL_BOX = (
    int(_CANVAS_SIZE[0] * 0.1), 1420, int(_CANVAS_SIZE[0] * 0.9), 1760
)
_ROWS_FILL_BOX = (_TEXT_INSET_X, 1780, int(_CANVAS_SIZE[0] * 0.75), 2360)


def plate_for(fill_lux):
    """(plate_rgba, text_rgba) for a fill of luminance `fill_lux` (0..1).

    Light fill -> strong dark plate + white text; dark fill -> ~92%-opaque
    white plate + near-black text. Constancy: both fixed pairs pass >= 7:1
    contrast for every fill input; only the polarity flips.
    """
    if fill_lux >= _LIGHT_FILL_LUX:
        return _DARK_PLATE
    return _LIGHT_PLATE


def _canvas_base(colors):
    """Fallback canvas fill: theme background, then primary, else white."""
    return colors.get('background') or colors.get('primary') or '#FFFFFF'


def _srgb_to_linear(channel):
    """Linearise one sRGB channel value (0..1) per WCAG 2.x."""
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color):
    """WCAG relative luminance of a #RRGGBB hex string (0..1)."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def _region_lux(img, box):
    """Mean WCAG linear luminance (0..1) of an image region.

    Samples RGB channels (BOX-downsampled then linearised): `L`-grey conversion
    would mis-weight saturated fills like pure red (cr review, T3).
    """
    small = img.crop(box).resize((64, 32), Image.Resampling.BOX)
    pixels = small.load()
    total = 0.0
    for py in range(32):
        for px in range(64):
            r, g, b = pixels[px, py][:3]
            total += (
                0.2126 * _srgb_to_linear(r / 255)
                + 0.7152 * _srgb_to_linear(g / 255)
                + 0.0722 * _srgb_to_linear(b / 255)
            )
    return total / (64 * 32)


def _fill_from_image(img):
    """Mean linear luminance (0..1) of the strip the name plate will cover."""
    return _region_lux(img, _PLATE_FILL_BOX)


def _apply_pixel_ceiling(img, what):
    """Reject images whose declared dimensions exceed a decode-safe ceiling.

    A sub-cap byte limit can still decompress to a huge canvas; the ceiling is
    checked on the header BEFORE `img.load()` (cr finding, T3).
    """
    width, height = img.size
    if width * height > _MAX_ASSET_PIXELS or max(width, height) > _MAX_ASSET_DIM:
        raise ThemeAssetError(
            f'theme {what} rejected: {width}x{height} exceeds pixel ceiling'
        )


def _decode_background(raw):
    """Open + gate background art: opaque, >= 1600x1130, landscape."""
    img = Image.open(io.BytesIO(raw))
    _apply_pixel_ceiling(img, 'background')
    img.load()
    width, height = img.size
    if width < _MIN_BG_SIZE[0] or height < _MIN_BG_SIZE[1] or width <= height:
        raise ThemeAssetError(
            f'theme background rejected: {width}x{height} below '
            f'{_MIN_BG_SIZE[0]}x{_MIN_BG_SIZE[1]} landscape'
        )
    if img.mode != 'RGB':
        if 'transparency' in img.info or img.mode in ('RGBA', 'LA'):
            flattened = Image.new('RGBA', img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(flattened, img.convert('RGBA'))
        img = img.convert('RGB')
    return img


def _fallback_canvas(colors):
    """Plain 3508x2480 fill from the theme colours + a subtle two-tone band."""
    img = Image.new('RGB', _CANVAS_SIZE, _canvas_base(colors))
    accent = colors.get('accent')
    if accent:
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, _CANVAS_SIZE[0], 28), fill=accent)
        draw.rectangle((0, _CANVAS_SIZE[1] - 28, _CANVAS_SIZE[0], _CANVAS_SIZE[1]), fill=accent)
    return img


def _themed_background(theme, deadline=None):
    """(canvas, fill_lux) — validated background art, or theme-coloured fallback.

    Never raises: any fetch/validation/decode failure logs a warning and uses
    the plain theme-coloured fallback canvas (spec §7 never-fail contract).
    """
    url = theme.get('certificate_background_url')
    if url:
        try:
            raw = fetch_theme_asset(url, max_bytes=MAX_BG_BYTES, deadline=deadline)
            fitted = ImageOps.fit(
                _decode_background(raw), _CANVAS_SIZE, Image.Resampling.LANCZOS
            )
            return fitted, _fill_from_image(fitted)
        except Exception as exc:  # noqa: BLE001 — background: fallback, never fail
            _log.warning(
                'theme background rejected, fallback canvas (%s: %s)',
                type(exc).__name__, exc
            )
    colors = theme.get('colors') or {}
    return _fallback_canvas(colors), _relative_luminance(_canvas_base(colors))


def _font(name, size):
    """DejaVu font, degrading to Pillow's default when unavailable."""
    try:
        return ImageFont.truetype(_FONT_DIR + name, size)
    except Exception:  # noqa: BLE001 — bitmap fallback keeps rendering alive
        return ImageFont.load_default(size)


def _ellipsize(draw, text, font, max_width):
    """Truncate `text` with an ellipsis so it fits `max_width` pixels.

    Linear by construction (PR12 review): one full-width measure, a single
    proportional cut against the mean glyph advance, then one final
    re-measure that trims only any residual kerning excess — replacing the
    shrink-by-one-remeasure loop that was O(n²) on hostile input. Ellipsis
    semantics unchanged: unchanged when it already fits, else a `…`-suffixed
    prefix that fits `max_width`.
    """
    text = str(text)
    full = draw.textlength(text, font=font)
    if full <= max_width:
        return text
    ell = draw.textlength('…', font=font)
    budget = max_width - ell
    if budget <= 0:
        return '…'
    mean = full / len(text)
    cut = f'{text[:int(budget / mean)]}…'
    width = draw.textlength(cut, font=font)  # single final re-measure
    while width > max_width and len(cut) > 1:
        # Residual non-uniform glyph widths: trim proportionally to the
        # measured excess instead of one char per pass.
        cut = cut[max(1, int((width - max_width) / mean)):]
        width = draw.textlength(cut, font=font)
    return cut


def _draw_text_plate(img, draw, text, font, fill_lux, *, center=None, pos=None, pad=_PLATE_PAD):
    """Composite text on an auto-polarity plate (>= 7:1 for any fill).

    Default placement centers on the name plate anchor; `center` re-centers
    for heading/bib plates, `pos` pins the plate's top-left (metadata rows).
    """
    plate_rgba, text_rgba = plate_for(fill_lux)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    pad_x, pad_y = pad
    plate = Image.new(
        'RGBA', (right - left + 2 * pad_x, bottom - top + 2 * pad_y), plate_rgba
    )
    plate_draw = ImageDraw.Draw(plate)
    plate_draw.text((pad_x - left, pad_y - top), text, font=font, fill=text_rgba)
    if pos is None:
        center = center or _PLATE_CENTER
        pos = (center[0] - plate.width // 2, center[1] - plate.height // 2)
    img.paste(plate, pos, plate)


def _paste_asset(img, url, *, max_frac, x, y, right_edge=False, deadline=None):
    """Fetch + composite one thumbnail with a thin keyline at `max_frac` width."""
    raw = fetch_theme_asset(url, max_bytes=MAX_SMALL_ASSET_BYTES, deadline=deadline)
    icon = Image.open(io.BytesIO(raw))
    _apply_pixel_ceiling(icon, 'thumbnail')
    icon = icon.convert('RGBA')
    max_w = int(_CANVAS_SIZE[0] * max_frac)
    if icon.width > max_w or icon.height > _ASSET_MAX_H:
        scale = min(max_w / icon.width, _ASSET_MAX_H / icon.height)
        icon = icon.resize(
            # aspect-preserving; width stays <= max_frac, height <= _ASSET_MAX_H
            (
                max(1, round(icon.width * scale)),
                max(1, round(icon.height * scale))
            ),
            Image.Resampling.LANCZOS
        )
    paste_x = x - icon.width if right_edge else x
    ImageDraw.Draw(img).rectangle(
        (paste_x - 3, y - 3, paste_x + icon.width + 2, y + icon.height + 2),
        outline=(255, 255, 255)
    )
    img.paste(icon, (paste_x, y), icon)


def _meta_rows(labels, meta):
    """Field rows: values with resolved labels when provided (spec §7)."""
    if labels.get('run'):
        rows = [f"{labels['run']}: {meta['event_name']}"]
    else:
        rows = [str(meta['event_name'])]
    if labels.get('bib'):
        rows.append(f"{labels['bib']}: {meta['bib_number']}")
    if labels.get('participant'):
        rows.append(f"{labels['participant']}: {meta['runner_name']}")
    rows.append(str(meta['event_date']))
    return rows


def _render_themed(theme, meta, deadline=None):
    """A4-landscape (3508x2480) themed render (spec §7).

    Background validation / remote-asset failures degrade per asset; only
    unexpected programming errors propagate to `_render_safe`. `deadline`
    (optional `time.monotonic()` instant) is the render's shared budget: each
    asset fetch stops at min(its own 60s, remaining).
    """
    labels = theme.get('labels') or {}
    layout = theme.get('layout') or {}

    img, fill_lux = _themed_background(theme, deadline)
    draw = ImageDraw.Draw(img)
    fonts = {
        'heading': _font('DejaVuSans-Bold.ttf', 150),
        'name': _font('DejaVuSans-Bold.ttf', 220),
        'bib': _font('DejaVuSans-Bold.ttf', 240),
        'row': _font('DejaVuSans.ttf', 100),
    }

    # Every text field sits on an auto-polarity backing plate so the >= 7:1
    # contrast contract holds even over hostile mixed-luminance art (cr, T3).
    if labels.get('certificate'):
        heading = _ellipsize(
            draw, labels['certificate'], fonts['heading'],
            int(_CANVAS_SIZE[0] * _PLATE_MAX_FRAC)
        )
        _draw_text_plate(
            img, draw, heading, fonts['heading'],
            _region_lux(img, _HEADING_FILL_BOX),
            center=_HEADING_CENTER, pad=_SMALL_PLATE_PAD
        )

    plate_text_w = int(_CANVAS_SIZE[0] * _PLATE_MAX_FRAC) - 2 * _PLATE_PAD[0]
    name = _ellipsize(draw, meta['runner_name'], fonts['name'], plate_text_w)
    _draw_text_plate(img, draw, name, fonts['name'], fill_lux)

    bib = _ellipsize(draw, meta['bib_number'], fonts['bib'], int(_CANVAS_SIZE[0] * 0.8))
    _draw_text_plate(
        img, draw, bib, fonts['bib'],
        _region_lux(img, _BIB_FILL_BOX),
        center=_BIB_CENTER, pad=_SMALL_PLATE_PAD
    )

    row_lux = _region_lux(img, _ROWS_FILL_BOX)
    row_w = _CANVAS_SIZE[0] - 2 * (_TEXT_INSET_X + _SMALL_PLATE_PAD[0])
    pad_x, pad_y = _SMALL_PLATE_PAD
    y = _ROWS_TOP
    for text in _meta_rows(labels, meta):
        drawn = _ellipsize(draw, text, fonts['row'], row_w)
        _draw_text_plate(
            img, draw, drawn, fonts['row'], row_lux,
            pos=(_TEXT_INSET_X - pad_x, y - pad_y), pad=_SMALL_PLATE_PAD
        )
        y += _ROW_STEP

    for key, max_frac, x, right_edge in (
        ('logo_url', _LOGO_MAX_FRAC, _TEXT_INSET_X, False),
        ('badge_icon_url', _BADGE_MAX_FRAC, _CANVAS_SIZE[0] - _TEXT_INSET_X, True),
    ):
        url = layout.get(key)
        if not url:
            continue
        try:
            _paste_asset(img, url, max_frac=max_frac, x=x, y=_SAFE_MARGIN * 2,
                         right_edge=right_edge, deadline=deadline)
        except Exception as exc:  # noqa: BLE001 — optional asset: omit, never fail
            _log.warning('theme %s omitted (%s: %s)', key, type(exc).__name__, exc)

    return img


def _render_safe(theme, meta):
    """Never-fail themed render (spec §7): warn + legacy fallback, bubble nothing."""
    # One shared deadline for the whole render (PR12 review): sequential
    # per-asset 60s budgets could otherwise stack to ~180s and outlive the
    # gunicorn worker timeout; each fetch stops at min(60s, remaining).
    deadline = time.monotonic() + _RENDER_DEADLINE_SECONDS
    try:
        return _render_themed(theme, meta, deadline=deadline)
    except Exception as exc:  # noqa: BLE001 — never-fail contract
        _log.warning(
            'themed render failed (%s: %s), legacy fallback', type(exc).__name__, exc
        )
        return _render_legacy(
            meta['runner_name'], meta['event_name'], meta['bib_number'], meta['event_date']
        )


def _render_legacy(runner_name, event_name, bib_number, event_date):
    """Legacy 800x600 render, extracted verbatim (Task 1 seam)."""
    template_path = os.path.join(
        os.path.dirname(__file__), 'templates', 'cert_template.png'
    )

    if not os.path.exists(template_path):
        img = Image.new('RGB', (800, 600), color='white')
    else:
        img = Image.open(template_path).convert('RGB')

    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 60
        )
        small_font = ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 40
        )
    except Exception:  # noqa: BLE001 — legacy font fallback keeps render alive
        font = ImageFont.load_default()
        small_font = font

    draw.text(
        (img.width // 2, 200),
        runner_name,
        fill='black',
        font=font,
        anchor='mm'
    )
    draw.text(
        (img.width // 2, 320),
        event_name,
        fill='#555555',
        font=small_font,
        anchor='mm'
    )
    draw.text(
        (img.width // 2, 420),
        bib_number,
        fill='#888888',
        font=small_font,
        anchor='mm'
    )
    draw.text(
        (img.width // 2, 520),
        event_date,
        fill='#888888',
        font=small_font,
        anchor='mm'
    )

    return img
