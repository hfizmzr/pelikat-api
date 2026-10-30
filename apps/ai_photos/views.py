"""
AI Photo Processing Views
"""

import uuid

from postgrest.exceptions import APIError
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .reader import read_bibs_from_bytes
from .services import (
    RACE_PHOTOS_BUCKET,
    NoPhotosFound,
    enqueue_batch,
    enqueue_prefix,
    get_batch_status,
    read_storage_photo,
)

BATCH_ID_FIELDS = ('batch_id', 'event_id', 'organizer_id')


def is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def missing_batch_ids(data) -> list[str]:
    return [field for field in BATCH_ID_FIELDS if not is_uuid(data.get(field))]


def enqueue_error_response(exc: Exception) -> Response:
    # enqueue_photo_batch raises 22023 for an empty path list. Fixed text:
    # never echo database / library messages to the caller.
    if isinstance(exc, APIError) and exc.code == '22023':
        return Response({'error': 'No photos to process.'}, status=400)
    raise exc


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
        except Exception as exc:  # noqa: BLE001 — single unreadable file must not kill a batch upload
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

    Queues the photos for the background worker (manage.py run_photo_worker)
    and returns immediately.

    Request body:
    {
        "batch_id": "uuid",
        "event_id": "uuid",
        "organizer_id": "uuid",
        "created_by": "uuid (optional)",
        "storage_paths": ["user_id/event_id/batch_id/img001.jpg", ...]
    }

    Response (202): the photo_batches row
    { "id": "uuid", "status": "queued", "total": 12, "processed": 0, ... }
    """
    data = request.data
    missing = missing_batch_ids(data)
    if missing:
        return Response({'error': f"{', '.join(missing)} must be UUIDs"}, status=400)

    paths = data.get('storage_paths') or []
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
        return Response({'error': 'storage_paths must be a non-empty list of paths'}, status=400)

    try:
        batch = enqueue_batch(
            batch_id=data['batch_id'],
            event_id=data['event_id'],
            organizer_id=data['organizer_id'],
            storage_paths=paths,
            created_by=data.get('created_by') if is_uuid(data.get('created_by')) else None,
        )
    except APIError as exc:
        return enqueue_error_response(exc)
    return Response(batch, status=202)


@api_view(['POST'])
def process_photo_prefix(request):
    """
    POST /ai/photos/process-prefix

    Lists every image under the storage prefix and queues them as one
    reprocess batch. Returns immediately.

    Request body:
    {
        "batch_id": "uuid",
        "event_id": "uuid",
        "organizer_id": "uuid",
        "created_by": "uuid (optional)",
        "bucket": "race-photos",
        "prefix": "user_id/event_id"
    }

    Response (202): the photo_batches row
    """
    data = request.data
    missing = missing_batch_ids(data)
    if missing:
        return Response({'error': f"{', '.join(missing)} must be UUIDs"}, status=400)

    prefix = data.get('prefix')
    if not prefix:
        return Response({'error': 'prefix is required'}, status=400)
    if not isinstance(prefix, str):
        return Response({'error': 'prefix must be a string'}, status=400)

    # The worker only reads from race-photos, so a batch listed from any
    # other bucket would be processed against the wrong objects.
    bucket = data.get('bucket') or RACE_PHOTOS_BUCKET
    if bucket != RACE_PHOTOS_BUCKET:
        return Response({'error': f'Only the {RACE_PHOTOS_BUCKET} bucket can be processed'}, status=400)

    try:
        batch = enqueue_prefix(
            batch_id=data['batch_id'],
            event_id=data['event_id'],
            organizer_id=data['organizer_id'],
            bucket=bucket,
            prefix=prefix,
            created_by=data.get('created_by') if is_uuid(data.get('created_by')) else None,
        )
    except NoPhotosFound:
        return Response({'error': 'No photos found in that upload folder.'}, status=400)
    except APIError as exc:
        return enqueue_error_response(exc)
    return Response(batch, status=202)


@api_view(['GET'])
def photo_status(request, batch_id):
    """
    GET /ai/photos/status/{batch_id}

    Response: the photo_batches row
    { "id": "uuid", "status": "processing", "total": 12, "processed": 5,
      "auto_tagged": 3, "flagged": 1, "no_bib": 0, "failed": 1, ... }
    """
    if not is_uuid(batch_id):
        return Response({'error': 'batch_id must be a UUID'}, status=400)

    batch = get_batch_status(batch_id)
    if batch is None:
        return Response({'error': 'Batch not found'}, status=404)
    return Response(batch)
