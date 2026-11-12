"""Covers: FR-23–FR-27 (UC06 REPC check-in, UC07 proxy collection), NFR07.
Layers: SQL text pins + endpoint tests. Tests: 15.

REPC / QR check-in & proxy collection tests (FR-23..27).

Layering note (verified against the codebase, 2026-11-11):
- The REPC check-in itself is a Supabase RPC, NOT Django code. The web app
  calls ``repc_check_in_with_merch`` (063), which wraps the live
  ``repc_check_in_registration`` (latest create-or-replace: 091, earlier
  revisions 028/037/042/046) and ``merch_collect_for_runner`` in one
  transaction. Consent requirement, already-checked-in idempotency,
  one-per-registration uniqueness, shirt stock decrement, the
  repc_collections insert shape and the audit triggers are all SQL-owned.
- Django (apps/qr_security) only participates in: HMAC-SHA256 QR
  sign/verify (services.py:18-73), the 6-char consent code
  (services.py:76-84) and issuing consent codes with a 24h TTL while
  marking prior unused codes used (views.py:90-95).
- The web layer (pelikat-web/src/app/api/repc/check-in and
  proxy-check-in routes) verifies the QR via /ai/qr/verify and then calls
  the RPC, writing ``runner_check_in`` audit_log rows and the collector /
  absent-runner metadata (collector_* columns, notes, used_by_*) itself.

So: SQL-owned behaviors are pinned below with source-text assertions on
the migration files (mirroring test_audit_log_append_only.py's
source-structure style); Django-owned behaviors are tested through the
real endpoints, only adding what existing suites (test_qr_signing,
test_consent_code) do not already cover.
"""

from pathlib import Path

import pytest

from apps.qr_security.services import sign_qr

# pelikat-api/apps/tests/test_repc_collection.py -> workspace root
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = WORKSPACE_ROOT / 'pelikat-web' / 'supabase' / 'migrations'


def _migration(filename: str) -> str:
    path = MIGRATIONS / filename
    assert path.exists(), f"migration file missing: {path}"
    return path.read_text()


M028 = '028_setup_repc_shirt_inventory_checkin.sql'
M032 = '032_create_audit_triggers.sql'
M037 = '037_fix_repc_check_in_ambiguous.sql'
M042 = '042_repc_collection_log_fields.sql'
M063 = '063_merch_review_fixes.sql'
# 091 carries the live (final) repc_check_in_registration body; 028/037/042/046
# are superseded create-or-replaces of the same function.
M091 = '091_repc_require_active_organizer.sql'


@pytest.mark.security
class TestRepcCheckInSql:
    """Pin SQL-owned invariants of repc_check_in_registration / with_merch.

    SQL-text spec pinning: the RPC is created-or-replaced across
    migrations and the live body is 091; these assertions pin the live
    definition plus the structural invariants 028/037/042 introduced.
    Layer ownership: SQL (Supabase). Django never calls these functions.
    """

    def test_consent_required_for_proxy(self):
        """FR-23: proxy collection must carry a consent code (091 live body)."""
        sql = _migration(M091)
        assert 'if p_is_proxy then' in sql
        assert "'Proxy collection requires a consent code'" in sql

    def test_consent_code_must_be_unused_and_unexpired(self):
        """FR-24: consent lookup requires is_used=false AND expires_at > now().

        Expired or already-used codes therefore fall into the single
        'Invalid or expired consent code' rejection branch.
        """
        sql = _migration(M091)
        assert "cc.code = upper(trim(p_consent_code))" in sql
        assert 'cc.is_used = false' in sql
        assert 'cc.expires_at > now()' in sql
        assert "'Invalid or expired consent code'" in sql

    def test_already_checked_in_is_idempotent(self):
        """FR-25: re-scan of a checked-in runner returns success=true with
        already_checked_in=true and exits BEFORE the consent check, stock
        decrement and repc_collections insert (no duplicate rows).
        """
        sql = _migration(M091)
        already = sql.index('if v_reg.checked_in then')
        insert = sql.index('insert into public.repc_collections')
        consent = sql.index('if p_is_proxy then')
        assert 0 < already < consent < insert
        assert "'Runner is already checked in'" in sql
        assert 'true::boolean, v_reg.remaining_qty' not in sql  # sanity: exact text below
        assert 'v_reg.t_shirt_size, v_remaining, true::boolean' in sql

    def test_one_per_registration_uniqueness(self):
        """FR-25: a registration can never get two repc_collections rows.

        028 created the unique index; 037 replaced it with a named unique
        constraint that 091's insert uses as ON CONFLICT target.
        """
        assert 'repc_collections_one_per_registration_idx' in _migration(M028)
        assert 'add constraint repc_collections_registration_id_unique unique (registration_id)' in _migration(M037)
        assert 'on conflict on constraint repc_collections_registration_id_unique do nothing' in _migration(M091)

    def test_collection_insert_shape(self):
        """FR-26: repc_collections insert fields pinned (091 live body).

        Collector identity comes from p_collected_by (web passes
        'Name (BIB xxx)' for proxy, user.email/id for self) and
        collected_by_user_id from auth.uid(); collection timestamp is
        SQL-owned now(); proxy runs get collection_method='proxy' plus a
        'Proxy collection by ...' notes marker.
        """
        sql = _migration(M091)
        insert = sql.index('insert into public.repc_collections')
        values = sql.index('values (', insert)
        end = sql.index('on conflict', values)
        insert_sql = sql[insert:end]
        for column in (
            'registration_id',
            'organizer_id',
            'collected_by',
            'collected_at',
            'is_proxy',
            'consent_code_id',
            'shirt_size',
            'shirt_inventory_id',
            'collected_by_user_id',
            'collection_method',
            'qr_verified_at',
            'remaining_qty_after',
            'staff_email',
            'notes',
        ):
            assert column in insert_sql, f"missing insert column: {column}"
        assert "case when p_is_proxy then 'proxy' else 'self' end" in sql
        assert "'Proxy collection by ' || coalesce(p_collected_by, auth.uid()::text)" in sql
        assert 'coalesce(p_collected_by, auth.uid()::text)' in sql
        assert 'now()' in insert_sql

    def test_shirt_stock_decrement_and_exhaustion(self):
        """FR-27: shirt inventory decrement is SQL-owned.

        Only rows with remaining stock can check in; claimed_qty is
        atomically incremented and the post-decrement remainder returned.
        """
        sql = _migration(M091)
        assert 'set claimed_qty = claimed_qty + 1' in sql
        assert 'if v_remaining <= 0 then' in sql
        assert "'No shirts remaining for this size'" in sql
        assert 'returning initial_qty - claimed_qty into v_remaining' in sql

    def test_active_organizer_guard(self):
        """Expired organizers cannot run REPC check-in (091 live body)."""
        sql = _migration(M091)
        assert "'Organizer subscription is not active'" in sql
        assert 'o.is_active' in sql

    def test_consent_used_audit_fields(self):
        """Consent-code consumption is audited: is_used + used_at stamped
        inside the RPC (091) and 042 added used_by_* columns that the web
        proxy route fills with the collector identity.
        """
        sql = _migration(M091)
        assert 'set is_used = true' in sql
        assert 'used_at = now()' in sql
        sql042 = _migration(M042)
        assert 'used_at timestamptz' in sql042
        assert 'used_by_runner_id' in sql042
        assert 'used_by_registration_id' in sql042

    def test_audit_triggers_on_repc_tables(self):
        """Audit is SQL-owned: append triggers on repc_collections and
        repc_consent_codes (032). The web layer only adds its own
        runner_check_in audit_log rows on top.
        """
        sql = _migration(M032)
        assert 'create trigger audit_repc_collections' in sql
        assert 'create trigger audit_repc_consent_codes' in sql

    def test_check_in_with_merch_wraps_checkin_atomically(self):
        """063: repc_check_in_with_merch delegates to
        repc_check_in_registration (which holds the registration row
        lock) and hands over merch in the SAME transaction; re-scans of
        already-checked-in runners still hand over merch orders marked
        ready after the first scan.
        """
        sql = _migration(M063)
        assert 'create or replace function public.repc_check_in_with_merch' in sql
        assert 'from public.repc_check_in_registration(' in sql
        assert 'from public.merch_collect_for_runner(p_event_id, p_runner_id) m' in sql
        assert 'if coalesce(v_check.success, false) then' in sql
        # merch_collect_for_runner is internal-only (revoked from
        # authenticated); it refuses runners not checked in at REPC.
        assert "revoke all on function public.merch_collect_for_runner(uuid, uuid) from public, anon, authenticated" in sql
        assert "'Runner has not checked in at REPC for this event'" in sql


@pytest.mark.security
class TestQrVerifyForCollection:
    """Django-owned QR verify path used by both REPC routes.

    The web app POSTs the scanned QR to /ai/qr/verify before calling the
    RPC; only endpoint-level additions missing from test_qr_signing.py
    are covered here.
    """

    def test_verify_endpoint_rejects_tampered_collection_token(self, api_client, internal_api_key):
        """A tampered collection QR must be rejected by the endpoint the
        REPC routes call, with the fixed 'Invalid signature' error.
        """
        token = sign_qr('runner-123', 'event-456', 'B001')
        payload_b64, _sig = token.split('.')

        response = api_client.post(
            '/ai/qr/verify',
            data={'qr_payload': f'{payload_b64}.deadbeef'},
            content_type='application/json',
            **internal_api_key,
        )

        assert response.status_code == 200
        data = response.json()
        assert data['valid'] is False
        assert data['error'] == 'Invalid signature'

    def test_verify_endpoint_rejects_malformed_payload(self, api_client, internal_api_key):
        """Garbage input to the verify endpoint yields the generic
        'invalid payload' error, never a stack trace or exception.
        """
        response = api_client.post(
            '/ai/qr/verify',
            data={'qr_payload': 'not-a-token'},
            content_type='application/json',
            **internal_api_key,
        )

        assert response.status_code == 200
        data = response.json()
        assert data['valid'] is False
        assert data['error'] == 'invalid payload'

    def test_verify_endpoint_returns_runner_details_for_valid_token(self, api_client, internal_api_key):
        """The fields the REPC routes consume (runner_id, event_id,
        bib_number) round-trip through the endpoint.
        """
        token = sign_qr('runner-123', 'event-456', 'B001')

        response = api_client.post(
            '/ai/qr/verify',
            data={'qr_payload': token},
            content_type='application/json',
            **internal_api_key,
        )

        assert response.status_code == 200
        data = response.json()
        assert data['valid'] is True
        assert data['runner_id'] == 'runner-123'
        assert data['event_id'] == 'event-456'
        assert data['bib_number'] == 'B001'


@pytest.mark.security
class TestConsentLayerOwnership:
    """Pin which layer owns consent consumption.

    Django only ISSUES consent codes (24h TTL, single-use on re-issue,
    both covered by test_consent_code.py). The expired/used rejection at
    collection time is SQL-owned (pinned above via TestRepcCheckInSql);
    there must be no Django-side consumption of consent codes or call
    into the check-in RPC — the web layer calls the RPC directly.
    """

    def test_qr_security_views_do_not_consume_consent_codes(self):
        """qr_security views must not validate/consume consent codes or
        invoke the check-in RPC — that belongs to SQL + web layer.
        """
        import inspect

        from apps.qr_security import views

        source = inspect.getsource(views)
        assert 'repc_check_in' not in source
        assert 'repc_collections' not in source
        # Consent issuance is the only repc_consent_codes touch, and it
        # only marks prior unused codes used on re-issue (single-use).
        assert source.count('repc_consent_codes') == 2

    def test_consent_reissue_marks_previous_unused_codes_used(self, api_client, internal_api_key):
        """Endpoint-level pin (reuses test_consent_code.py mocking
        pattern): re-issuing a consent code marks prior unused codes
        is_used=True — the Django side of single-use enforcement.
        """
        from unittest.mock import MagicMock, patch

        with patch('apps.qr_security.views._supabase') as mock_sb:
            mock_sb.return_value.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value = MagicMock(
                data={'id': 'reg-uuid', 'organizer_id': 'org-uuid'}
            )
            mock_sb.return_value.table.return_value.insert.return_value.execute.return_value = MagicMock()

            response = api_client.post(
                '/ai/qr/consent-code',
                data={'registration_id': 'reg-uuid'},
                content_type='application/json',
                **internal_api_key,
            )

            assert response.status_code == 200
            # views.py:92-95: update({'is_used': True}) filtered on
            # registration_id + is_used=False — prior codes die on re-issue.
            update_mock = mock_sb.return_value.table.return_value.update
            update_mock.assert_called_once_with({'is_used': True})
            # Chained filter: .eq('registration_id', reg).eq('is_used', False).
            first_eq = update_mock.return_value.eq.call_args
            second_eq = update_mock.return_value.eq.return_value.eq.call_args
            assert first_eq.args == ('registration_id', 'reg-uuid')
            assert second_eq.args == ('is_used', False)
