"""
Background photo pipeline (FR-16) and batch stats (FR-20): enqueue views,
per-photo outcomes, worker crash handling, paginated storage listing.
"""
from unittest.mock import MagicMock, patch

import pytest
from postgrest.exceptions import APIError
from rest_framework.test import APIRequestFactory

from apps.ai_photos import services, views

BATCH = '11111111-1111-1111-1111-111111111111'
EVENT = '22222222-2222-2222-2222-222222222222'
ORG = '33333333-3333-3333-3333-333333333333'
USER = '44444444-4444-4444-4444-444444444444'

factory = APIRequestFactory()


def post(view, body):
    return view(factory.post('/', body, format='json'))


# ── photo_outcome ───────────────────────────────────────────────

@pytest.mark.parametrize('rows, kept, expected', [
    ([{'bib_number': '1', 'status': 'auto'}, {'bib_number': '2', 'status': 'review'}], [], 'flagged'),
    ([{'bib_number': '1', 'status': 'auto'}], [], 'auto_tagged'),
    ([], [{'bib_number': '1', 'status': 'confirmed'}], 'auto_tagged'),
    ([{'bib_number': None, 'status': 'discarded'}], [], 'no_bib'),
    ([], [{'bib_number': '9', 'status': 'discarded'}], 'no_bib'),
])
def test_photo_outcome(rows, kept, expected):
    assert services.photo_outcome(rows, kept) == expected


# ── enqueue views ───────────────────────────────────────────────

def test_process_enqueues_and_returns_202():
    batch = {'id': BATCH, 'status': 'queued', 'total': 2}
    with patch('apps.ai_photos.views.enqueue_batch', return_value=batch) as enqueue:
        res = post(views.process_photos, {
            'batch_id': BATCH, 'event_id': EVENT, 'organizer_id': ORG, 'created_by': USER,
            'storage_paths': ['u/e/b/a.jpg', 'u/e/b/b.jpg'],
        })

    assert res.status_code == 202
    assert res.data == batch
    enqueue.assert_called_once_with(
        batch_id=BATCH, event_id=EVENT, organizer_id=ORG,
        storage_paths=['u/e/b/a.jpg', 'u/e/b/b.jpg'], created_by=USER,
    )


@pytest.mark.parametrize('body', [
    {'batch_id': 'nope', 'event_id': EVENT, 'organizer_id': ORG, 'storage_paths': ['a.jpg']},
    {'batch_id': BATCH, 'event_id': EVENT, 'storage_paths': ['a.jpg']},
    {'batch_id': BATCH, 'event_id': EVENT, 'organizer_id': ORG, 'storage_paths': []},
    {'batch_id': BATCH, 'event_id': EVENT, 'organizer_id': ORG, 'storage_paths': 'a.jpg'},
])
def test_process_rejects_bad_input(body):
    with patch('apps.ai_photos.views.enqueue_batch') as enqueue:
        res = post(views.process_photos, body)

    assert res.status_code == 400
    enqueue.assert_not_called()


def test_process_maps_empty_batch_db_error_to_400():
    error = APIError({'code': '22023', 'message': 'No photos to process.', 'details': None, 'hint': None})
    with patch('apps.ai_photos.views.enqueue_batch', side_effect=error):
        res = post(views.process_photos, {
            'batch_id': BATCH, 'event_id': EVENT, 'organizer_id': ORG, 'storage_paths': [' '],
        })

    assert res.status_code == 400
    assert res.data == {'error': 'No photos to process.'}


def test_process_prefix_with_no_photos_is_400():
    with patch('apps.ai_photos.services.list_storage_image_paths', return_value=[]), \
         patch('apps.ai_photos.services.enqueue_batch') as enqueue:
        res = post(views.process_photo_prefix, {
            'batch_id': BATCH, 'event_id': EVENT, 'organizer_id': ORG, 'prefix': 'u/e',
        })

    assert res.status_code == 400
    enqueue.assert_not_called()


def test_process_prefix_enqueues_reprocess_batch():
    with patch('apps.ai_photos.services.list_storage_image_paths', return_value=['u/e/b/a.jpg']), \
         patch('apps.ai_photos.services.enqueue_batch', return_value={'id': BATCH}) as enqueue:
        res = post(views.process_photo_prefix, {
            'batch_id': BATCH, 'event_id': EVENT, 'organizer_id': ORG, 'prefix': 'u/e',
        })

    assert res.status_code == 202
    assert enqueue.call_args.kwargs['source'] == 'reprocess'
    assert enqueue.call_args.kwargs['prefix'] == 'u/e'


def test_status_404_for_unknown_batch():
    with patch('apps.ai_photos.views.get_batch_status', return_value=None):
        res = views.photo_status(factory.get('/'), batch_id=BATCH)
    assert res.status_code == 404


# ── worker ──────────────────────────────────────────────────────

def test_run_queue_item_records_crash_as_failed():
    item = {'id': 'item-1', 'storage_path': 'a.jpg', 'event_id': EVENT, 'organizer_id': ORG, 'batch_id': BATCH}
    with patch('apps.ai_photos.services.process_photo', side_effect=RuntimeError('model exploded')), \
         patch('apps.ai_photos.services.finish_queue_item') as finish:
        services.run_queue_item(item)

    assert finish.call_args.args == ('item-1', None)
    assert finish.call_args.kwargs['error'] == 'model exploded'


def test_drain_queue_stops_when_asked():
    with patch('apps.ai_photos.services.claim_queue_items') as claim:
        assert services.drain_queue(should_stop=lambda: True) == 0
    claim.assert_not_called()


# ── storage listing ─────────────────────────────────────────────

def test_list_storage_image_paths_pages_past_1000():
    page_size = services.STORAGE_LIST_PAGE_SIZE
    first_page = [{'name': f'{i}.jpg'} for i in range(page_size)]
    second_page = [{'name': 'last.jpg'}]

    storage = MagicMock()
    storage.list.side_effect = [first_page, second_page]
    sb = MagicMock()
    sb.storage.from_.return_value = storage

    with patch('apps.ai_photos.services.get_supabase_client', return_value=sb):
        paths = services.list_storage_image_paths('race-photos', 'u/e/b')

    assert len(paths) == page_size + 1
    assert paths[-1] == 'u/e/b/last.jpg'
    assert storage.list.call_args_list[1].args[1] == {'limit': page_size, 'offset': page_size}


# ── speed: shared client, bib index, warm-up ───────────────────

def test_drain_queue_reuses_one_client_and_one_bib_index_per_event():
    items = [
        {'id': f'item-{n}', 'storage_path': f'{n}.jpg', 'event_id': EVENT, 'organizer_id': ORG, 'batch_id': BATCH}
        for n in range(3)
    ]
    sb = MagicMock()
    with patch('apps.ai_photos.services.get_supabase_client', return_value=sb) as get_sb, \
         patch('apps.ai_photos.services.load_bib_index', return_value='index') as load, \
         patch('apps.ai_photos.services.claim_queue_items', side_effect=[items, []]), \
         patch('apps.ai_photos.services.finish_queue_item') as finish, \
         patch('apps.ai_photos.services.process_photo',
               return_value={'outcome': 'auto_tagged', 'saved_tags': 1}) as process:
        assert services.drain_queue() == 3

    get_sb.assert_called_once()
    load.assert_called_once_with(sb, EVENT)
    assert all(call.kwargs == {'sb': sb, 'bib_index': 'index'} for call in process.call_args_list)
    assert all(call.kwargs['sb'] is sb for call in finish.call_args_list)


def test_bib_index_matches_exact_then_case_insensitive():
    index = services.BibIndex([
        {'id': 'r1', 'bib_number': 'm90038'},
        {'id': 'r2', 'bib_number': 'M90038'},
        {'id': 'r3', 'bib_number': None},
    ])
    assert index.find('M90038')['id'] == 'r2'
    assert index.find('m90038')['id'] == 'r1'
    assert services.BibIndex([{'id': 'r1', 'bib_number': 'm90038'}]).find('M90038')['id'] == 'r1'
    assert index.find('99999') is None
    assert index.find(None) is None


def test_load_bib_index_pages_past_1000():
    page_size = services.REGISTRATION_PAGE_SIZE
    pages = [
        [{'id': f'r{i}', 'runner_id': None, 'bib_number': str(i)} for i in range(page_size)],
        [{'id': 'last', 'runner_id': None, 'bib_number': 'LAST1'}],
    ]
    sb = MagicMock()
    query = sb.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.order.return_value
    query.range.return_value.execute.side_effect = [MagicMock(data=page) for page in pages]

    index = services.load_bib_index(sb, EVENT)

    assert index.find('LAST1')['id'] == 'last'
    assert index.find('0')['id'] == 'r0'
    assert [call.args for call in query.range.call_args_list] == [(0, page_size - 1), (page_size, 2 * page_size - 1)]


def test_bib_index_cache_reloads_after_ttl():
    now = [0.0]
    with patch('apps.ai_photos.services.load_bib_index', side_effect=['first', 'second']) as load:
        cache = services.BibIndexCache(MagicMock(), ttl_seconds=300, clock=lambda: now[0])
        assert cache.get(EVENT) == 'first'
        now[0] = 299
        assert cache.get(EVENT) == 'first'
        now[0] = 301
        assert cache.get(EVENT) == 'second'
    assert load.call_count == 2


def test_build_rows_uses_bib_index_instead_of_queries():
    result = {
        'storage_path': 'u/e/b/a.jpg',
        'detections': [{'bib_number': 'M90038', 'ocr_confidence': 0.95, 'status': 'auto'}],
    }
    index = services.BibIndex([{'id': 'reg-1', 'runner_id': 'runner-1', 'bib_number': 'M90038'}])
    with patch('apps.ai_photos.services.find_registration_for_bib') as lookup:
        rows = services.build_photo_tag_rows(result, EVENT, ORG, BATCH, MagicMock(), index)

    lookup.assert_not_called()
    assert rows[0]['registration_id'] == 'reg-1'
    assert rows[0]['status'] == 'auto'


def test_warm_up_runs_both_models_once():
    with patch('apps.ai_photos.reader.get_yolo_model') as yolo, \
         patch('apps.ai_photos.reader.get_ocr_reader') as ocr:
        from apps.ai_photos.reader import warm_up_models
        warm_up_models()

    yolo.return_value.predict.assert_called_once()
    ocr.return_value.ocr.assert_called_once()


@pytest.mark.parametrize('saved, outcome, saved_tags', [
    # new read flagged next to a kept confirmed tag
    ([{'status': 'review', 'bib_number': '7', 'inserted': True},
      {'status': 'confirmed', 'bib_number': '5', 'inserted': False}], 'flagged', 1),
    # every read was already confirmed, so nothing new was written
    ([{'status': 'confirmed', 'bib_number': '5', 'inserted': False}], 'auto_tagged', 0),
    # only the "nothing found" placeholder
    ([{'status': 'discarded', 'bib_number': None, 'inserted': True}], 'no_bib', 1),
])
def test_process_photo_outcome_comes_from_replace_result(saved, outcome, saved_tags):
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value.data = saved
    with patch('apps.ai_photos.services.read_storage_photo',
               return_value={'storage_path': 'a.jpg', 'detections': []}):
        result = services.process_photo('a.jpg', EVENT, ORG, BATCH, sb=sb, bib_index=services.BibIndex([]))

    assert result['outcome'] == outcome
    assert result['saved_tags'] == saved_tags
