"""
AI Photo Processing Views
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response
import threading

from .services import process_photo as process_photo_service


@api_view(['POST'])
def process_photos(request):
    """
    POST /ai/photos/process

    Request body:
    {
        "batch_id": "uuid",
        "event_id": "uuid",
        "organizer_id": "uuid",
        "storage_paths": ["race-photos/event123/img001.jpg", ...]
    }

    Response:
    { "batch_id": "uuid", "queued": 12 }
    """
    data = request.data
    batch_id = data.get('batch_id')
    event_id = data.get('event_id')
    organizer_id = data.get('organizer_id')
    paths = data.get('storage_paths', [])

    for path in paths:
        t = threading.Thread(
            target=process_photo_service,
            args=(path, event_id, organizer_id, batch_id)
        )
        t.daemon = True
        t.start()

    return Response({'batch_id': batch_id, 'queued': len(paths)})


@api_view(['GET'])
def photo_status(request, batch_id):
    """
    GET /ai/photos/status/{batch_id}

    Response:
    { "batch_id": "uuid", "counts": {...} }
    """
    from .services import get_batch_status
    result = get_batch_status(batch_id)
    return Response(result)