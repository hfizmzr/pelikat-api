"""
AI Photo Processing Views
"""

from rest_framework.decorators import api_view
from rest_framework.response import Response

from .reader import read_bibs_from_bytes
from .services import read_storage_photo
from .services import process_storage_paths, process_storage_prefix


def get_bib_options(data):
    return {
        'bib_regex': data.get('bib_regex') or None,
        'bib_format': data.get('bib_format') or None,
    }


@api_view(['POST'])
def read_uploaded_photos(request):
    """
    POST /ai/photos/read

    Multipart form fields:
    - images: one or more image files
    - bib_regex: optional regex, for example ^[A-Z]{2}\\d{4}$
    - bib_format: optional shorthand, for example AA9999 or A-999

    Response:
    { "count": 2, "results": [...] }
    """
    files = request.FILES.getlist('images') or request.FILES.getlist('files')
    single_file = request.FILES.get('image')
    if single_file:
        files.append(single_file)

    if not files:
        return Response({'error': 'Upload at least one image using images, files, or image.'}, status=400)

    options = get_bib_options(request.data)
    results = []
    for image_file in files:
        try:
            result = read_bibs_from_bytes(
                image_file.read(),
                source_name=image_file.name,
                **options,
            )
        except Exception as exc:
            result = {'source_name': image_file.name, 'error': str(exc)}
        results.append(result)

    return Response({'count': len(results), 'results': results})


@api_view(['POST'])
def read_storage_photos(request):
    """
    POST /ai/photos/read-storage

    JSON body:
    {
        "bucket": "race-photos",
        "storage_paths": ["event123/img001.jpg"],
        "bib_format": "AA9999"
    }

    Response:
    { "count": 1, "results": [...] }
    """
    data = request.data
    paths = data.get('storage_paths') or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return Response({'error': 'storage_paths must contain at least one path.'}, status=400)

    bucket = data.get('bucket') or 'race-photos'
    options = get_bib_options(data)
    results = [
        read_storage_photo(path, bucket=bucket, **options)
        for path in paths
    ]
    return Response({'count': len(results), 'results': results})


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
    { "batch_id": "uuid", "processed": 12, "saved_tags": 18, "results": [...] }
    """
    data = request.data
    batch_id = data.get('batch_id')
    event_id = data.get('event_id')
    organizer_id = data.get('organizer_id')
    paths = data.get('storage_paths', [])

    result = process_storage_paths(paths, event_id, organizer_id, batch_id)

    return Response(result)


@api_view(['POST'])
def process_photo_prefix(request):
    """
    POST /ai/photos/process-prefix

    Request body:
    {
        "batch_id": "uuid",
        "event_id": "uuid",
        "organizer_id": "uuid",
        "bucket": "race-photos",
        "prefix": "user_id/event_id"
    }

    Response:
    { "batch_id": "uuid", "processed": 12, "saved_tags": 18, "results": [...] }
    """
    data = request.data
    batch_id = data.get('batch_id')
    event_id = data.get('event_id')
    organizer_id = data.get('organizer_id')
    bucket = data.get('bucket') or 'race-photos'
    prefix = data.get('prefix')

    if not prefix:
        return Response({'error': 'prefix is required'}, status=400)

    result = process_storage_prefix(
        bucket=bucket,
        prefix=prefix,
        event_id=event_id,
        organizer_id=organizer_id,
        batch_id=batch_id,
    )
    return Response(result)


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
