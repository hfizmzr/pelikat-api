"""
E-Certificate Views
"""

import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import generate_cert

_log = logging.getLogger('ecert')

# Render-input caps (PR12 review): _ellipsize is linear but the draw cost of
# unbounded strings is not, and a bib is a prefix + number. Truncate (the API
# has always returned 200 for any string) and warn at the 'ecert' logger.
_TEXT_CAPS = {'runner_name': 256, 'event_name': 256, 'bib_number': 32}


def _cap_field(name, value):
    """Truncate an over-long string field to its cap, warning when it bites."""
    cap = _TEXT_CAPS[name]
    if isinstance(value, str) and len(value) > cap:
        _log.warning('%s truncated to %d chars (over-long input)', name, cap)
        return value[:cap]
    return value


@api_view(['POST'])
def generate_cert_view(request):
    """
    POST /ai/ecert/generate

    Request:
    {
        "runner_id": "uuid",
        "event_id": "uuid",
        "registration_id": "uuid",
        "runner_name": "John Doe",
        "event_name": "KL Marathon 2025",
        "bib_number": "A001",
        "event_date": "2025-03-15",
        "theme": {"colors": {...}, "labels": {...}, ...}
    }

    Response:
    { "cert_url": "https://...supabase.co/storage/v1/object/sign/..." }
    """
    data = request.data

    runner_name = data.get('runner_name')
    event_name = data.get('event_name')
    bib_number = data.get('bib_number')
    event_date = data.get('event_date')
    registration_id = data.get('registration_id')

    theme = data.get('theme')
    if theme is not None and not isinstance(theme, dict):
        return Response(
            {'detail': "theme must be an object (dict) or null"},
            status=400
        )

    if not all([runner_name, event_name, bib_number, event_date, registration_id]):
        return Response({'error': 'Missing required fields'}, status=400)

    runner_name = _cap_field('runner_name', runner_name)
    event_name = _cap_field('event_name', event_name)
    bib_number = _cap_field('bib_number', bib_number)

    try:
        cert_url = generate_cert(
            runner_name=runner_name,
            event_name=event_name,
            bib_number=bib_number,
            event_date=event_date,
            registration_id=registration_id,
            theme=theme
        )
        return Response({'cert_url': cert_url})
    except Exception as e:
        # 500 is an internal failure — the raw exception detail must not leak
        # to the client; log it server-side (with traceback) instead.
        _log.exception('ecert render failed: %s', e)
        return Response({'error': 'internal error'}, status=500)