"""
E-Certificate Generation Service

Uses Pillow to generate PNG e-certificates and upload to Supabase Storage.
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont
from supabase import create_client
from django.conf import settings


def get_supabase_client():
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY
    )


def generate_cert(
    runner_name: str,
    event_name: str,
    bib_number: str,
    event_date: str,
    registration_id: str
) -> str:
    """
    Generate PNG e-cert, upload to Supabase Storage, return signed URL.

    Args:
        runner_name: Full name of the runner
        event_name: Name of the event
        bib_number: Bib number assigned
        event_date: Date of the event (string)
        registration_id: UUID of the registration

    Returns:
        Signed URL (24h expiry)
    """
    sb = get_supabase_client()

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