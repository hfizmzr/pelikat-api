"""
AI Photo Processing Service

Uses YOLO for bib detection and PaddleOCR for OCR on cropped regions.

Batches are queued in photo_batches / photo_batch_items (migration 087)
and processed one photo at a time by `manage.py run_photo_worker`, so no
HTTP request waits on the models.
"""

import logging
import time

from pelikat.supabase import get_sb as get_supabase_client

from .reader import read_bibs_from_bytes

logger = logging.getLogger(__name__)

# The only bucket the queue reads from (process_photo -> read_storage_photo).
RACE_PHOTOS_BUCKET = 'race-photos'
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
STORAGE_LIST_PAGE_SIZE = 1000
REGISTRATION_PAGE_SIZE = 1000
# How long the worker trusts an event's bib list before reloading it.
BIB_INDEX_TTL_SECONDS = 300
# A bib not in the cached list triggers a reload at most this often, so a
# BIB assigned mid-batch is matched within seconds without a query per
# unmatched read (spectator / banner numbers miss all the time).
BIB_MISS_RELOAD_SECONDS = 30


class NoPhotosFound(Exception):
    """A reprocess prefix contained no images."""


def read_storage_photo(
    storage_path: str,
    *,
    bucket: str = RACE_PHOTOS_BUCKET,
    bib_regex: str | None = None,
    bib_format: str | None = None,
    sb=None,
) -> dict:
    """
    Download a photo from Supabase Storage and read bib candidates.

    This deliberately does not write photo_tags yet. Tag persistence can be
    added once the Supabase table structure is finalized.
    """
    sb = sb or get_supabase_client()

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
        offset = 0
        while True:
            entries = storage.list(folder, {'limit': STORAGE_LIST_PAGE_SIZE, 'offset': offset})
            for entry in entries:
                name = entry.get('name')
                if not name:
                    continue

                path = f"{folder.rstrip('/')}/{name}" if folder else name
                if name.lower().endswith(IMAGE_EXTENSIONS):
                    image_paths.append(path)
                else:
                    walk(path)

            if len(entries) < STORAGE_LIST_PAGE_SIZE:
                return
            offset += STORAGE_LIST_PAGE_SIZE

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


class BibIndex:
    """
    An event's registrations by bib, loaded once instead of 1-2 queries per
    detected bib. Same matching as find_registration_for_bib: exact first,
    then case-insensitive.
    """

    def __init__(self, registrations: list[dict]):
        self.exact = {}
        self.folded = {}
        for registration in registrations:
            bib_number = registration.get('bib_number')
            if bib_number:
                self.exact.setdefault(bib_number, registration)
                self.folded.setdefault(bib_number.upper(), registration)

    def find(self, bib_number: str) -> dict | None:
        if not bib_number:
            return None
        return self.exact.get(bib_number) or self.folded.get(bib_number.upper())


def load_bib_index(sb, event_id: str) -> BibIndex:
    registrations = []
    start = 0
    while True:
        page = (
            sb.table('registrations')
            .select('id, runner_id, bib_number')
            .eq('event_id', event_id)
            .not_.is_('bib_number', 'null')
            .order('id')
            .range(start, start + REGISTRATION_PAGE_SIZE - 1)
            .execute()
        ).data or []
        registrations.extend(page)
        if len(page) < REGISTRATION_PAGE_SIZE:
            return BibIndex(registrations)
        start += REGISTRATION_PAGE_SIZE


class BibIndexCache:
    """
    Per-event BibIndex for the worker. Entries expire after ttl_seconds; a
    lookup miss reloads the event's list if it is older than
    miss_reload_seconds. Expired entries are dropped on every get, so a long
    drain over many events doesn't keep them all.
    """

    def __init__(
        self,
        sb,
        ttl_seconds: float = BIB_INDEX_TTL_SECONDS,
        miss_reload_seconds: float = BIB_MISS_RELOAD_SECONDS,
        clock=time.monotonic,
    ):
        self.sb = sb
        self.ttl_seconds = ttl_seconds
        self.miss_reload_seconds = miss_reload_seconds
        self.clock = clock
        self.entries: dict[str, tuple[float, BibIndex]] = {}

    def load(self, event_id: str) -> BibIndex:
        index = load_bib_index(self.sb, event_id)
        self.entries[event_id] = (self.clock(), index)
        return index

    def get(self, event_id: str) -> BibIndex:
        now = self.clock()
        for key in [key for key, (loaded_at, _) in self.entries.items() if now - loaded_at > self.ttl_seconds]:
            del self.entries[key]

        entry = self.entries.get(event_id)
        return entry[1] if entry else self.load(event_id)

    def find(self, event_id: str, bib_number: str) -> dict | None:
        registration = self.get(event_id).find(bib_number)
        if registration or not bib_number:
            return registration

        loaded_at, _ = self.entries[event_id]
        if self.clock() - loaded_at < self.miss_reload_seconds:
            return None
        return self.load(event_id).find(bib_number)

    def for_event(self, event_id: str) -> 'EventBibs':
        return EventBibs(self, event_id)


class EventBibs:
    """BibIndex-shaped view of one event in a BibIndexCache (refreshes on a miss)."""

    def __init__(self, cache: BibIndexCache, event_id: str):
        self.cache = cache
        self.event_id = event_id

    def find(self, bib_number: str) -> dict | None:
        return self.cache.find(self.event_id, bib_number)


def build_photo_tag_rows(
    result: dict,
    event_id: str,
    organizer_id: str,
    batch_id: str,
    sb,
    bib_index: BibIndex | EventBibs | None = None,
) -> list[dict]:
    detections_by_bib = {}
    for detection in result.get('detections', []):
        bib_number = detection.get('bib_number')
        # Below the review threshold is noise (FR-19 flags 0.50 and up).
        if not bib_number or detection.get('status') == 'discarded':
            continue

        existing = detections_by_bib.get(bib_number)
        if existing is None or detection.get('ocr_confidence', 0) > existing.get('ocr_confidence', 0):
            detections_by_bib[bib_number] = detection

    rows = []
    for bib_number, detection in detections_by_bib.items():
        registration = (
            bib_index.find(bib_number) if bib_index else find_registration_for_bib(sb, event_id, bib_number)
        )
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


def replace_ai_tags(sb, event_id: str, storage_path: str, rows: list[dict]) -> list[dict]:
    """
    Swap the photo's AI-owned tags for `rows` in one transaction
    (replace_ai_photo_tags, migration 088). Organizer decisions are kept:
    confirmed tags, and discarded reads an organizer rejected (reviewed_at
    set); new reads for those bibs are skipped. Returns every row the photo
    now has, each flagged `inserted`.
    """
    res = sb.rpc('replace_ai_photo_tags', {
        'p_event_id': event_id,
        'p_storage_path': storage_path,
        'p_rows': rows,
    }).execute()
    return res.data or []


def photo_outcome(rows: list[dict], kept: list[dict]) -> str:
    """Per-photo batch outcome (FR-20): any flagged tag wins over auto-tagged."""
    statuses = {row['status'] for row in rows if row.get('bib_number')}
    if 'review' in statuses:
        return 'flagged'
    if 'auto' in statuses or any(tag.get('status') == 'confirmed' for tag in kept):
        return 'auto_tagged'
    return 'no_bib'


def process_photo(
    storage_path: str,
    event_id: str,
    organizer_id: str,
    batch_id: str,
    *,
    sb=None,
    bib_index: BibIndex | EventBibs | None = None,
):
    """
    Download a photo, read bib candidates, and persist rows to photo_tags.

    Reprocessing replaces only AI-owned rows; confirmed and rejected tags
    survive (see replace_ai_tags). The worker passes a shared client and
    the event's BibIndex; other callers get fresh lookups.
    """
    sb = sb or get_supabase_client()
    result = read_storage_photo(storage_path, sb=sb)
    if result.get('error'):
        return result

    try:
        rows = build_photo_tag_rows(result, event_id, organizer_id, batch_id, sb, bib_index)
        saved = replace_ai_tags(sb, event_id, storage_path, rows)
    except Exception as e:  # noqa: BLE001 — per-photo graceful degrade; error dict keeps the batch alive
        return {'storage_path': storage_path, 'error': str(e)}

    inserted = [tag for tag in saved if tag.get('inserted')]
    kept = [tag for tag in saved if not tag.get('inserted')]
    result.update(
        {
            'event_id': event_id,
            'organizer_id': organizer_id,
            'batch_id': batch_id,
            'saved_tags': len(inserted),
            'outcome': photo_outcome(inserted, kept),
        }
    )
    return result


def enqueue_batch(
    *,
    batch_id: str,
    event_id: str,
    organizer_id: str,
    storage_paths: list[str],
    created_by: str | None = None,
    source: str = 'upload',
    prefix: str | None = None,
) -> dict:
    """Queue a batch for the worker. Re-posting the same batch_id returns the existing batch."""
    sb = get_supabase_client()
    res = sb.rpc('enqueue_photo_batch', {
        'p_batch_id': batch_id,
        'p_event_id': event_id,
        'p_organizer_id': organizer_id,
        'p_created_by': created_by,
        'p_source': source,
        'p_prefix': prefix,
        'p_paths': storage_paths,
    }).execute()
    return res.data[0]


def enqueue_prefix(
    *,
    batch_id: str,
    event_id: str,
    organizer_id: str,
    bucket: str,
    prefix: str,
    created_by: str | None = None,
) -> dict:
    storage_paths = list_storage_image_paths(bucket, prefix)
    if not storage_paths:
        raise NoPhotosFound(prefix)

    return enqueue_batch(
        batch_id=batch_id,
        event_id=event_id,
        organizer_id=organizer_id,
        storage_paths=storage_paths,
        created_by=created_by,
        source='reprocess',
        prefix=prefix,
    )


def get_batch_status(batch_id: str) -> dict | None:
    """Persisted status and per-photo counters for a batch, or None if unknown."""
    sb = get_supabase_client()
    res = sb.table('photo_batches').select('*').eq('id', batch_id).limit(1).execute()
    return res.data[0] if res.data else None


def claim_queue_items(limit: int = 1, *, sb=None) -> list[dict]:
    sb = sb or get_supabase_client()
    return sb.rpc('claim_photo_batch_items', {'p_limit': limit}).execute().data or []


def finish_queue_item(item_id: str, outcome: str | None, error: str | None = None, tag_count: int = 0, *, sb=None):
    sb = sb or get_supabase_client()
    sb.rpc('finish_photo_batch_item', {
        'p_item_id': item_id,
        'p_outcome': outcome,
        'p_error': error,
        'p_tag_count': tag_count,
    }).execute()


def run_queue_item(item: dict, *, sb=None, bib_indexes: BibIndexCache | None = None) -> dict:
    """Process one claimed photo and record its outcome on the batch."""
    sb = sb or get_supabase_client()
    try:
        bib_index = bib_indexes.for_event(item['event_id']) if bib_indexes else None
        result = process_photo(
            item['storage_path'], item['event_id'], item['organizer_id'], item['batch_id'],
            sb=sb, bib_index=bib_index,
        )
    except Exception as e:  # noqa: BLE001 — a crash on one photo is recorded as failed, the worker moves on
        result = {'storage_path': item['storage_path'], 'error': str(e)}

    if result.get('error'):
        logger.warning('photo %r failed: %s', item['storage_path'], result['error'])
        finish_queue_item(item['id'], None, error=result['error'], sb=sb)
    else:
        finish_queue_item(item['id'], result['outcome'], tag_count=result.get('saved_tags', 0), sb=sb)
    return result


def drain_queue(*, limit: int = 1, should_stop=lambda: False) -> int:
    """
    Process claimed items until the queue is empty. Returns how many photos
    were handled. One Supabase client (one HTTPS connection) and one bib
    cache serve the whole drain.
    """
    sb = get_supabase_client()
    bib_indexes = BibIndexCache(sb)
    handled = 0
    while not should_stop():
        items = claim_queue_items(limit, sb=sb)
        if not items:
            return handled
        for item in items:
            run_queue_item(item, sb=sb, bib_indexes=bib_indexes)
            handled += 1
    return handled
