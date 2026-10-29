"""
E-Certificate Generation Service

Uses Pillow to generate PNG e-certificates and upload to Supabase Storage.
"""

import io
import logging
import os
import re

from PIL import Image, ImageDraw, ImageFont
from supabase import create_client
from django.conf import settings

_log = logging.getLogger('ecert')

_HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}$')

_COLOR_KEYS = ('primary', 'secondary', 'accent', 'background', 'foreground')
_LABEL_KEYS = ('bib', 'checkpoint', 'medal', 'run', 'participant', 'certificate')
_LABEL_ALIASES = {
    'bib_label': 'bib',
    'checkpoint_label': 'checkpoint',
    'medal_label': 'medal',
    'run_label': 'run',
    'participant_label': 'participant',
    'certificate_label': 'certificate',
}
_LAYOUT_KEYS = ('logo_url', 'badge_icon_url', 'name_plate')


def get_supabase_client():
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY
    )


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
        canonical = _LABEL_ALIASES.get(sk) if key == 'labels' else None
        if canonical is None and sk in section_keys:
            canonical = sk
        if canonical is None:
            _log.warning('theme %s: dropped unknown key %r', key, sk)
            continue
        if key == 'layout' and sk == 'name_plate':
            if sv != 'auto':
                _log.warning('theme layout: ignored name_plate %r (kept auto)', sv)
            continue
        cleaned_value = clean_value(key, canonical, sv)
        if cleaned_value is None:
            continue
        if key == 'labels' and len(cleaned_value) > 64:
            _log.warning('theme labels: dropped %r (over 64 chars)', canonical)
            continue
        cleaned[canonical] = cleaned_value
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
    """Render the certificate image. Themed rendering consumes `theme` (Task 3)."""
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
    except Exception:
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
