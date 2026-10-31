"""
Reprocessing keeps organizer decisions (confirmed / rejected tags).

The keep / replace rules run inside replace_ai_photo_tags (migration 088,
one transaction); these tests cover what process_photo sends it and how it
reads the result.
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


def run_process(saved_rows, read_result=READ_RESULT):
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value.data = saved_rows
    with patch('apps.ai_photos.services.get_supabase_client', return_value=sb), \
         patch('apps.ai_photos.services.read_storage_photo', return_value=dict(read_result)), \
         patch('apps.ai_photos.services.find_registration_for_bib',
               side_effect=lambda _sb, _event, bib: {'id': f'reg-{bib}', 'runner_id': f'runner-{bib}'}):
        result = process_photo(read_result['storage_path'], 'event-1', 'org-1', 'batch-2')
    return result, sb


def sent_rows(sb):
    name, params = sb.rpc.call_args.args
    assert name == 'replace_ai_photo_tags'
    assert params['p_event_id'] == 'event-1'
    assert params['p_storage_path'] == 'org/event/batch/a.jpg'
    return params['p_rows']


def test_replaces_tags_in_one_rpc_call_never_separate_delete_insert():
    _, sb = run_process([])

    sb.rpc.assert_called_once()
    sb.table.return_value.delete.assert_not_called()
    sb.table.return_value.insert.assert_not_called()
    rows = sent_rows(sb)
    assert {row['bib_number'] for row in rows} == {'101', '202', '303'}
    assert all(row['batch_id'] == 'batch-2' and row['organizer_id'] == 'org-1' for row in rows)


def test_saved_tags_counts_only_rows_this_run_inserted():
    # 101 was confirmed and 202 rejected earlier: the function kept them and skipped those reads.
    saved = [
        {'status': 'auto', 'bib_number': '303', 'inserted': True},
        {'status': 'confirmed', 'bib_number': '101', 'inserted': False},
        {'status': 'discarded', 'bib_number': '202', 'inserted': False},
    ]
    result, _ = run_process(saved)

    assert result['saved_tags'] == 1


def test_db_error_returns_error_dict_instead_of_raising():
    with patch('apps.ai_photos.services.get_supabase_client') as get_sb, \
         patch('apps.ai_photos.services.read_storage_photo', return_value=dict(READ_RESULT)), \
         patch('apps.ai_photos.services.find_registration_for_bib', return_value=None):
        get_sb.return_value.rpc.return_value.execute.side_effect = Exception('replace failed')
        result = process_photo(READ_RESULT['storage_path'], 'event-1', 'org-1', 'batch-2')

    assert result == {'storage_path': READ_RESULT['storage_path'], 'error': 'replace failed'}


def test_lookup_error_never_reaches_the_database():
    with patch('apps.ai_photos.services.get_supabase_client') as get_sb, \
         patch('apps.ai_photos.services.read_storage_photo', return_value=dict(READ_RESULT)), \
         patch('apps.ai_photos.services.find_registration_for_bib', side_effect=Exception('lookup failed')):
        result = process_photo(READ_RESULT['storage_path'], 'event-1', 'org-1', 'batch-2')

    assert result['error'] == 'lookup failed'
    get_sb.return_value.rpc.assert_not_called()


def test_below_review_threshold_reads_are_not_saved():
    read_result = {
        'storage_path': 'org/event/batch/a.jpg',
        'detections': [{'bib_number': '999', 'ocr_confidence': 0.30, 'status': 'discarded'}],
    }
    _, sb = run_process([], read_result)

    # Only the bib-less "nothing found" placeholder is sent.
    assert [(row['bib_number'], row['status']) for row in sent_rows(sb)] == [(None, 'discarded')]
