"""
AI Photo Processing Service

Uses YOLO for bib detection and PaddleOCR for OCR on cropped regions.
"""

from pelikat.supabase import get_sb as get_supabase_client

from .reader import read_bibs_from_bytes

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')


def read_storage_photo(
    storage_path: str,
    *,
    bucket: str = 'race-photos',
    bib_regex: str | None = None,
    bib_format: str | None = None,
) -> dict:
    """
    Download a photo from Supabase Storage and read bib candidates.

    This deliberately does not write photo_tags yet. Tag persistence can be
    added once the Supabase table structure is finalized.
    """
    sb = get_supabase_client()

    try:
        image_bytes = sb.storage.from_(bucket).download(storage_path)
        result = read_bibs_from_bytes(
            image_bytes,
            source_name=storage_path,
            bib_regex=bib_regex,
            bib_format=bib_format,
        )
        result['storage_path'] = storage_path
        result['bucket'] = bucket
        return result

    except Exception as e:  # noqa: BLE001 — per-photo graceful degrade; error dict keeps the batch alive
        return {'storage_path': storage_path, 'error': str(e)}


def list_storage_image_paths(bucket: str, prefix: str) -> list[str]:
    sb = get_supabase_client()
    storage = sb.storage.from_(bucket)
    image_paths = []

    def walk(folder: str):
        entries = storage.list(folder, {'limit': 1000})
        for entry in entries:
            name = entry.get('name')
            if not name:
                continue

            path = f"{folder.rstrip('/')}/{name}" if folder else name
            if name.lower().endswith(IMAGE_EXTENSIONS):
                image_paths.append(path)
            else:
                walk(path)

    walk(prefix.strip('/'))
    return image_paths


def find_registration_for_bib(sb, event_id: str, bib_number: str) -> dict | None:
    if not bib_number:
        return None

    res = (
        sb.table('registrations')
        .select('id, runner_id, bib_number')
        .eq('event_id', event_id)
        .eq('bib_number', bib_number)
        .limit(1)
        .execute()
    )

    if res.data:
        return res.data[0]

    res = (
        sb.table('registrations')
        .select('id, runner_id, bib_number')
        .eq('event_id', event_id)
        .ilike('bib_number', bib_number)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def build_photo_tag_rows(result: dict, event_id: str, organizer_id: str, batch_id: str, sb) -> list[dict]:
    detections_by_bib = {}
    for detection in result.get('detections', []):
        bib_number = detection.get('bib_number')
        # Below the review threshold is noise; skipping it also keeps
        # discarded+bib rows exclusive to organizer rejections.
        if not bib_number or detection.get('status') == 'discarded':
            continue

        existing = detections_by_bib.get(bib_number)
        if existing is None or detection.get('ocr_confidence', 0) > existing.get('ocr_confidence', 0):
            detections_by_bib[bib_number] = detection

    rows = []
    for bib_number, detection in detections_by_bib.items():
        registration = find_registration_for_bib(sb, event_id, bib_number)
        status = detection.get('status') or 'review'
        if not registration:
            status = 'review'

        rows.append({
            'event_id': event_id,
            'organizer_id': organizer_id,
            'storage_path': result['storage_path'],
            'bib_number': bib_number,
            'registration_id': registration.get('id') if registration else None,
            'runner_id': registration.get('runner_id') if registration else None,
            'confidence': round(float(detection.get('ocr_confidence') or 0), 4),
            'status': status,
            'batch_id': batch_id,
        })

    if rows:
        return rows

    return [{
        'event_id': event_id,
        'organizer_id': organizer_id,
        'storage_path': result['storage_path'],
        'bib_number': None,
        'confidence': 0,
        'status': 'discarded',
        'batch_id': batch_id,
    }]


def is_organizer_decision(tag: dict) -> bool:
    """
    True for rows a reprocess must keep: organizer-confirmed tags, and
    rejected reads (discarded with a bib). The AI only ever writes
    'discarded' for the bib-less "nothing found" placeholder.
    """
    status = tag.get('status')
    return status == 'confirmed' or (status == 'discarded' and tag.get('bib_number') is not None)


def merge_with_kept_tags(rows: list[dict], kept: list[dict]) -> list[dict]:
    """Drop new rows that would duplicate or contradict a kept organizer decision."""
    if not kept:
        return rows

    kept_bibs = {tag['bib_number'] for tag in kept if tag.get('bib_number')}
    return [row for row in rows if row.get('bib_number') and row['bib_number'] not in kept_bibs]


def process_photo(storage_path: str, event_id: str, organizer_id: str, batch_id: str):
    """
    Download a photo, read bib candidates, and persist rows to photo_tags.

    Reprocessing replaces only AI-owned rows (auto / review / bib-less
    placeholder); confirmed and rejected tags survive.
    """
    sb = get_supabase_client()
    result = read_storage_photo(storage_path)
    if result.get('error'):
        return result

    try:
        # Build rows before deleting anything so a lookup failure leaves the old tags intact.
        rows = build_photo_tag_rows(result, event_id, organizer_id, batch_id, sb)
        existing = (
            sb.table('photo_tags')
            .select('id, bib_number, status')
            .eq('event_id', event_id)
            .eq('storage_path', storage_path)
            .execute()
        ).data or []
        kept = [tag for tag in existing if is_organizer_decision(tag)]
        replaceable_ids = [tag['id'] for tag in existing if not is_organizer_decision(tag)]
        rows = merge_with_kept_tags(rows, kept)

        if replaceable_ids:
            sb.table('photo_tags').delete().in_('id', replaceable_ids).execute()
        if rows:
            sb.table('photo_tags').insert(rows).execute()
    except Exception as e:  # noqa: BLE001 — per-photo graceful degrade; error dict keeps the batch alive
        return {'storage_path': storage_path, 'error': str(e)}

    result.update(
        {
            'event_id': event_id,
            'organizer_id': organizer_id,
            'batch_id': batch_id,
            'saved_tags': len(rows),
        }
    )
    return result


def process_storage_paths(
    storage_paths: list[str],
    event_id: str,
    organizer_id: str,
    batch_id: str,
) -> dict:
    results = [
        process_photo(storage_path, event_id, organizer_id, batch_id)
        for storage_path in storage_paths
    ]
    return {
        'batch_id': batch_id,
        'processed': len(storage_paths),
        'saved_tags': sum(result.get('saved_tags', 0) for result in results),
        'results': results,
    }


def process_storage_prefix(
    *,
    bucket: str,
    prefix: str,
    event_id: str,
    organizer_id: str,
    batch_id: str,
) -> dict:
    storage_paths = list_storage_image_paths(bucket, prefix)
    result = process_storage_paths(storage_paths, event_id, organizer_id, batch_id)
    result['bucket'] = bucket
    result['prefix'] = prefix
    return result


def get_batch_status(batch_id: str) -> dict:
    """Get persisted processing status for a batch."""
    sb = get_supabase_client()
    res = sb.table('photo_tags').select('status').eq('batch_id', batch_id).execute()

    counts = {'auto': 0, 'review': 0, 'discarded': 0, 'confirmed': 0, 'pending': 0}
    total = len(res.data)
    for row in res.data:
        status = row.get('status') or 'pending'
        counts[status] = counts.get(status, 0) + 1

    return {
        'batch_id': batch_id,
        'total': total,
        'counts': counts,
    }
