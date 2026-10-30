"""
Reprocessing keeps organizer decisions (confirmed / rejected tags).
"""
from unittest.mock import MagicMock, patch

from apps.ai_photos.services import process_photo

READ_RESULT = {
    'storage_path': 'org/event/batch/a.jpg',
    'detections': [
        {'bib_number': '101', 'ocr_confidence': 0.95, 'status': 'auto'},
        {'bib_number': '202', 'ocr_confidence': 0.70, 'status': 'review'},
        {'bib_number': '303', 'ocr_confidence': 0.90, 'status': 'auto'},
    ],
}


def run_process(existing_tags, read_result=READ_RESULT):
    sb = MagicMock()
    table = sb.table.return_value
    table.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = existing_tags
    with patch('apps.ai_photos.services.get_supabase_client', return_value=sb), \
         patch('apps.ai_photos.services.read_storage_photo', return_value=dict(read_result)), \
         patch('apps.ai_photos.services.find_registration_for_bib',
               side_effect=lambda _sb, _event, bib: {'id': f'reg-{bib}', 'runner_id': f'runner-{bib}'}):
        result = process_photo(read_result['storage_path'], 'event-1', 'org-1', 'batch-2')
    return result, table


def inserted_bibs(table):
    if not table.insert.called:
        return set()
    return {row['bib_number'] for row in table.insert.call_args.args[0]}


def test_reprocess_keeps_confirmed_and_rejected_tags():
    existing = [
        {'id': 't-confirmed', 'bib_number': '101', 'status': 'confirmed'},
        {'id': 't-rejected', 'bib_number': '202', 'status': 'discarded'},
        {'id': 't-auto', 'bib_number': '303', 'status': 'auto'},
        {'id': 't-review', 'bib_number': '404', 'status': 'review'},
        {'id': 't-placeholder', 'bib_number': None, 'status': 'discarded'},
    ]
    result, table = run_process(existing)

    table.delete.return_value.in_.assert_called_once_with('id', ['t-auto', 't-review', 't-placeholder'])
    # 101 is already confirmed and 202 was rejected — the AI must not re-add either.
    assert inserted_bibs(table) == {'303'}
    assert result['saved_tags'] == 1


def test_no_placeholder_when_photo_already_has_confirmed_tag():
    existing = [{'id': 't-confirmed', 'bib_number': '101', 'status': 'confirmed'}]
    result, table = run_process(existing, {'storage_path': 'org/event/batch/a.jpg', 'detections': []})

    table.delete.assert_not_called()
    table.insert.assert_not_called()
    assert result['saved_tags'] == 0


def test_first_run_inserts_all_reads():
    result, table = run_process([])

    table.delete.assert_not_called()
    assert inserted_bibs(table) == {'101', '202', '303'}


def test_db_error_returns_error_dict_instead_of_raising():
    sb_error = Exception('insert failed')
    with patch('apps.ai_photos.services.get_supabase_client') as get_sb, \
         patch('apps.ai_photos.services.read_storage_photo', return_value=dict(READ_RESULT)), \
         patch('apps.ai_photos.services.find_registration_for_bib', side_effect=sb_error):
        get_sb.return_value = MagicMock()
        result = process_photo(READ_RESULT['storage_path'], 'event-1', 'org-1', 'batch-2')

    assert result == {'storage_path': READ_RESULT['storage_path'], 'error': 'insert failed'}
    get_sb.return_value.table.return_value.delete.assert_not_called()


def test_below_review_threshold_reads_are_not_saved():
    read_result = {
        'storage_path': 'org/event/batch/a.jpg',
        'detections': [{'bib_number': '999', 'ocr_confidence': 0.30, 'status': 'discarded'}],
    }
    _, table = run_process([], read_result)

    # Only the bib-less "nothing found" placeholder is written.
    rows = table.insert.call_args.args[0]
    assert [(row['bib_number'], row['status']) for row in rows] == [(None, 'discarded')]
