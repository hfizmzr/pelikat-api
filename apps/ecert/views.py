"""
E-Certificate Views
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import generate_cert


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
        "event_date": "2025-03-15"
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

    if not all([runner_name, event_name, bib_number, event_date, registration_id]):
        return Response({'error': 'Missing required fields'}, status=400)

    try:
        cert_url = generate_cert(
            runner_name=runner_name,
            event_name=event_name,
            bib_number=bib_number,
            event_date=event_date,
            registration_id=registration_id
        )
        return Response({'cert_url': cert_url})
    except Exception as e:
        return Response({'error': str(e)}, status=500)