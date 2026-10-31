"""
HitPay payments flow tests: create / status / refund / reconcile,
plus internal-key middleware behaviour for the /payments/* surface.
"""
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from apps.payments import services
from apps.payments.hitpay import HitPayError

WEBHOOK_PATH = '/payments/webhook/hitpay/'
WEBHOOK_SALT = 'test-webhook-salt'


def _table(data=None):
    t = MagicMock()
    t.select.return_value.eq.return_value.limit.return_value.execute.return_value = \
        MagicMock(data=data if data is not None else [])
    t.select.return_value.or_.return_value.order.return_value.limit.return_value \
        .execute.return_value = MagicMock(data=data if data is not None else [])
    t.select.return_value.eq.return_value.lt.return_value.execute.return_value = \
        MagicMock(data=data if data is not None else [])
    t.select.return_value.eq.return_value.order.return_value.limit.return_value \
        .execute.return_value = MagicMock(data=data if data is not None else [])
    t.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    t.insert.return_value.execute.return_value = MagicMock(data=[])
    t.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    return t


def _mock_sb(**tables):
    sb = MagicMock()
    sb.table.side_effect = lambda name: tables.get(name, _table())
    return sb


def _pending_payment_table(rows):
    """Payments table whose select chain mirrors the create-idempotency lookup:
    select(*).eq(provider_request_id).limit(1).execute()."""
    t = MagicMock()
    t.select.return_value.eq.return_value.limit.return_value.execute.return_value = \
        MagicMock(data=rows)
    return t


CLAIM_NEW = [{
    'provider_request_id': 'pending:abc-def',
    'is_reservation': True,
}]


def _claim_sb(**tables):
    """Mock Supabase client whose claim_payment_slot RPC grants a fresh
    reservation slot (062 contract)."""
    sb = _mock_sb(**tables)
    sb.rpc.return_value.execute.return_value = MagicMock(
        data=[{'provider_request_id': 'pending:abc-def',
               'is_reservation': True}])
    return sb


def _claim_existing_sb(provider_request_id, **tables):
    """Mock Supabase client whose claim_payment_slot reports an existing
    (non-reservation) pending row for this order."""
    sb = _mock_sb(**tables)
    sb.rpc.return_value.execute.return_value = MagicMock(
        data=[{'provider_request_id': provider_request_id,
               'is_reservation': False}])
    return sb


def _auth(**extra):
    return {'HTTP_X_INTERNAL_KEY': 'test-internal-api-key-12345', **extra}


@pytest.mark.security
class TestPaymentsMiddleware:
    """'/payments/*' requires the internal key; the webhook path is exempt."""

    def test_payments_create_requires_internal_key(self, api_client):
        response = api_client.post(
            '/payments/create', data={}, content_type='application/json'
        )
        assert response.status_code == 401

    def test_payments_status_requires_internal_key(self, api_client):
        response = api_client.get('/payments/status?reference_number=reg-1')
        assert response.status_code == 401

    def test_webhook_path_exempt_from_internal_key(self, api_client):
        """No internal key on the webhook — signature decides (403, not 401)."""
        response = api_client.post(
            WEBHOOK_PATH, data=b'{}', content_type='application/json'
        )
        assert response.status_code == 403

    def test_create_with_key_passes_middleware(self, api_client):
        """With the key, middleware passes and the view validates the body."""
        response = api_client.post(
            '/payments/create', data={}, content_type='application/json', **_auth()
        )
        assert response.status_code == 400  # view validation, not 401


@pytest.mark.reliability
class TestCreatePayment:
    """Server-side price snapshot, 062 claim-serialization and HitPay payload
    construction."""

    def _payload(self, amount='50.00'):
        return {
            'order_type': 'registration',
            'registration_id': '11111111-1111-1111-1111-111111111111',
            'runner_id': '22222222-2222-2222-2222-222222222222',
            'amount': amount,
            'email': 'runner@example.com',
            'name': 'Runner One',
            'purpose': 'Event registration',
            'redirect_url': 'https://web.example.com/runner/payment?reg=x',
        }

    def test_create_ignores_client_amount_and_rejects_unpriced_order(self, api_client):
        """The caller's amount is never trusted: a registration derives its
        price server-side. A missing/zero server price → 422 Invalid amount,
        no HitPay call."""
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(registrations=_table([]),
                                         race_categories=_table([]))), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(amount='60.00'),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 422
        assert response.json()['error'] == 'Invalid amount'
        mock_create.assert_not_called()

    def test_create_server_price_wins_over_client_amount(self, api_client):
        """A lying or stale client amount cannot shift the charge —
        race_categories.price wins."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        payments = _table()
        sb = _claim_sb(registrations=reg, race_categories=cat, payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://x',
                                 'status': 'pending'}) as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(amount='1.99'),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 201
        assert mock_create.call_args.args[0]['amount'] == '50.00'

    def test_create_claim_runs_and_swaps_sentinel(self, api_client):
        """062: claim_payment_slot runs first; the reservation row's
        pending:<uuid> sentinel is replaced with the real PR id on success
        via a guarded update (no insert path remains)."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        payments = _table()
        sb = _claim_sb(registrations=reg, race_categories=cat, payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://x',
                                 'status': 'pending'}):
            response = api_client.post(
                '/payments/create', data=self._payload(amount='50.00'),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 201
        name, params = sb.rpc.call_args.args
        assert name == 'claim_payment_slot'
        assert params['p_amount'] == '50.00'
        assert params['p_currency'] == 'MYR'
        payments.insert.assert_not_called()
        update = payments.update.call_args.args[0]
        assert update['provider_request_id'] == 'pr-9'
        assert update['status'] == 'pending'

    def test_create_reservation_update_targets_sentinel_row(self, api_client):
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        payments = _table()
        sb = _claim_sb(registrations=reg, race_categories=cat, payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://x',
                                 'status': 'pending'}):
            api_client.post('/payments/create', data=self._payload(),
                            content_type='application/json', **_auth())
        eqs = [c.args for c in payments.update.return_value.eq.call_args_list]
        assert ('provider_request_id', 'pending:abc-def') in eqs

    def test_create_hitpay_failure_releases_reservation(self, api_client):
        """062: a HitPay create failure deletes the sentinel reservation row —
        the claim is released atomically and the next attempt starts fresh."""
        payments = _table()
        sb = _claim_sb(registrations=_table([{'category_id': 'c'}]),
                       race_categories=_table([{'price': '50.00'}]),
                       payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request',
                   side_effect=HitPayError('invalid amount', status=422)):
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 422
        eqs = [c.args for c in payments.delete.return_value.eq.call_args_list]
        assert ('provider_request_id', 'pending:abc-def') in eqs

    def test_create_no_claim_slot_409(self, api_client):
        """An empty claim (RPC grants nothing) → 409, no HitPay call."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        sb = _mock_sb(registrations=reg, race_categories=cat, payments=_table())
        sb.rpc.return_value.execute.return_value = MagicMock(data=[])
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 409
        mock_create.assert_not_called()

    def test_create_in_progress_claim_409(self, api_client):
        """063 tri-state: an in_progress claim (a sentinel row exists from an
        in-flight/crashed claim) → 409 fast-retry, never a second HitPay PR."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        sb = _mock_sb(registrations=reg, race_categories=cat, payments=_table())
        sb.rpc.return_value.execute.return_value = MagicMock(
            data=[{'provider_request_id': None, 'is_reservation': False,
                   'in_progress': True}])
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 409
        assert response.json()['error'] == \
            'Checkout already initializing; retry in a few seconds'
        mock_create.assert_not_called()

    def test_create_sends_expected_hitpay_fields(self, api_client):
        """Form payload: send_sms=false, allow_repeated_payments=false,
        reference_number=registration_id, no deprecated webhook field."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_claim_sb(registrations=reg, race_categories=cat,
                                          payments=_table())), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://x', 'status': 'pending'}) as mock_create:
            api_client.post('/payments/create', data=self._payload(),
                            content_type='application/json', **_auth())
        payload = mock_create.call_args.args[0]
        assert payload['reference_number'] == self._payload()['registration_id']
        assert payload['send_sms'] == 'false'
        assert payload['allow_repeated_payments'] == 'false'
        assert 'webhook' not in payload
        assert payload['amount'] == '50.00'
        assert payload['currency'] == 'MYR'
        assert not any(k.startswith('payment_methods') for k in payload)

    def test_create_env_override_sends_payment_methods(self, api_client):
        """HITPAY_PAYMENT_METHODS override → indexed form keys, normalized."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_claim_sb(registrations=reg, race_categories=cat,
                                          payments=_table())), \
             patch.object(services.settings, 'HITPAY_PAYMENT_METHODS',
                          'GrabPay_Direct, shopee_pay'), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://x',
                                 'status': 'pending'}) as mock_create:
            api_client.post('/payments/create', data=self._payload(),
                            content_type='application/json', **_auth())
        payload = mock_create.call_args.args[0]
        assert payload['payment_methods[0]'] == 'grabpay_direct'
        assert payload['payment_methods[1]'] == 'shopee_pay'

    def test_create_caller_supplied_methods_passed_through(self, api_client):
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_claim_sb(registrations=reg, race_categories=cat,
                                          payments=_table())), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://x',
                                 'status': 'pending'}) as mock_create:
            api_client.post(
                '/payments/create',
                data={**self._payload(), 'payment_methods': ['duitnow']},
                content_type='application/json', **_auth(),
            )
        payload = mock_create.call_args.args[0]
        assert payload['payment_methods[0]'] == 'duitnow'
        assert payload['generate_qr'] == 'true'  # single QR-capable rail

    def test_create_caller_empty_methods_omitted(self, api_client):
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_claim_sb(registrations=reg, race_categories=cat,
                                          payments=_table())), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://x',
                                 'status': 'pending'}) as mock_create:
            api_client.post(
                '/payments/create',
                data={**self._payload(), 'payment_methods': []},
                content_type='application/json', **_auth(),
            )
        assert not any(k.startswith('payment_methods')
                       for k in mock_create.call_args.args[0])

    def test_create_rejects_non_iso_currency(self, api_client):
        """A bad currency answers 422 to the caller instead of a HitPay crash."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_claim_sb(registrations=reg, race_categories=cat,
                                          payments=_table())), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create',
                data={**self._payload(), 'currency': 'XX'},
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 422
        assert 'currency' in response.json()['error'].lower()
        mock_create.assert_not_called()

    def test_create_subscription_uses_client_amount(self, api_client):
        """organizer_subscription has no server-side price source; the caller
        amount is used but recorded via the claim, never trusted blindly
        elsewhere."""
        payments = _table()
        sb = _claim_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-sub', 'url': 'https://x', 'status': 'pending'}):
            response = api_client.post(
                '/payments/create',
                data={
                    'order_type': 'organizer_subscription',
                    'organizer_id': '33333333-3333-3333-3333-333333333333',
                    'amount': '299.00',
                    'email': 'org@example.com',
                    'name': 'Organizer',
                    'purpose': 'Organizer subscription',
                    'redirect_url': 'https://web.example.com/organizer/payment',
                },
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 201
        _, params = sb.rpc.call_args.args
        assert params['p_amount'] == '299.00'
        assert params['p_order_type'] == 'organizer_subscription'
        payments.insert.assert_not_called()
        row = payments.update.call_args.args[0]
        assert row['provider_request_id'] == 'pr-sub'

    def test_create_subscription_rejects_nonpositive_amount(self, api_client):
        """062 hardening: subscription amounts must be positive — there is
        never a zero/invalid-charge path."""
        for bad in ('0', '-5', 'abc'):
            with patch('apps.payments.services.get_sb',
                       return_value=_mock_sb(payments=_table())), \
                 patch('apps.payments.services.create_payment_request') as mock_create:
                response = api_client.post(
                    '/payments/create',
                    data={
                        'order_type': 'organizer_subscription',
                        'organizer_id': '33333333-3333-3333-3333-333333333333',
                        'amount': bad,
                        'email': 'org@example.com', 'name': 'Organizer',
                        'redirect_url': 'https://x',
                    },
                    content_type='application/json', **_auth(),
                )
            assert response.status_code == 422, bad
            assert response.json()['error'] == 'Invalid amount'
            mock_create.assert_not_called()

    def test_create_idempotent_returns_existing_pending_pr(self, api_client):
        """M1/062: an is_reservation=false claim (existing pending PR) answers
        with the stored PR — re-fetched for a fresh checkout URL — and never
        creates a duplicate request against the (rate-limited) HitPay create
        endpoint."""
        payments = _pending_payment_table([{
            'provider_request_id': 'pr-old',
            'amount': '50.00',  # matches the server-derived price → reuse
            'status': 'pending',
            'qr_code_data': {'qr_code': 'qr-1'},
        }])
        with patch('apps.payments.services.get_sb',
                   return_value=_claim_existing_sb(
                       'pr-old',
                       registrations=_table([{'category_id': 'cat-1'}]),
                       race_categories=_table([{'price': '50.00'}]),
                       payments=payments)), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-old', 'url': 'https://checkout.hit-pay.com/old',
                                 'status': 'pending',
                                 'direct_link': {'direct_link_url': 'https://dl'}}), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 200
        body = response.json()
        assert body['provider_request_id'] == 'pr-old'
        assert body['checkout_url'] == 'https://checkout.hit-pay.com/old'
        assert body['qr_code'] == 'qr-1'
        assert body['status'] == 'pending'
        mock_create.assert_not_called()
        payments.update.assert_not_called()

    def test_create_idempotent_stale_amount_releases_and_creates_fresh(
            self, api_client):
        """Amount-aware reuse: a pending PR priced at the OLD amount (e.g.
        an admin changed the organizer subscription price from 149 → 99)
        must NOT keep serving the stale checkout. The stale row is released
        ('expired', guarded to still-pending) and a fresh PR is created at
        the CURRENT amount."""
        payments = _table([{
            'provider_request_id': 'pr-old',
            'amount': '149.00',
            'status': 'pending',
            'qr_code_data': None,
        }])
        sb = _mock_sb(payments=payments)
        # First claim → the stale existing pending row; after the guarded
        # release, the re-claim grants a fresh reservation slot.
        sb.rpc.return_value.execute.side_effect = [
            MagicMock(data=[{'provider_request_id': 'pr-old',
                             'is_reservation': False}]),
            MagicMock(data=[{'provider_request_id': 'pending:sentinel-1',
                             'is_reservation': True}]),
        ]
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-new', 'url': 'https://x',
                                 'status': 'pending'}) as mock_create, \
             patch('apps.payments.services.delete_payment_request'
                   ) as mock_delete:
            response = api_client.post(
                '/payments/create',
                data={
                    'order_type': 'organizer_subscription',
                    'organizer_id': '33333333-3333-3333-3333-333333333333',
                    'amount': '99.00',
                    'email': 'org@example.com', 'name': 'Organizer',
                    'purpose': 'Organizer subscription',
                    'redirect_url': 'https://web.example.com/organizer/payment',
                },
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 201
        body = response.json()
        assert body['provider_request_id'] == 'pr-new'
        # Fresh PR at the NEW amount (not the stale 149.00).
        assert mock_create.call_args.args[0]['amount'] == '99.00'
        update_payloads = [c.args[0] for c in payments.update.call_args_list]
        assert {'status': 'expired'} in update_payloads
        swap = next(u for u in update_payloads if 'provider_request_id' in u)
        assert swap['provider_request_id'] == 'pr-new'
        eqs = [c.args for c in payments.update.return_value.eq.call_args_list]
        # The release guard targets the stale row by id + still-pending.
        assert ('provider_request_id', 'pr-old') in eqs
        assert ('provider_request_id', 'pending:sentinel-1') in eqs
        # The superseded remote PR is cancelled (best-effort) so the old
        # checkout URL cannot still be paid after the local release.
        mock_delete.assert_called_once_with('pr-old')

    def test_create_stale_amount_delete_failure_is_tolerated(self, api_client):
        """Cancelling the superseded PR is best-effort: a HitPay error
        (e.g. 409 already-completed / timeout) must NOT fail the flow — the
        fresh claim still happens and the webhook owns a late completion."""
        payments = _table([{
            'provider_request_id': 'pr-old',
            'amount': '149.00',
            'status': 'pending',
            'qr_code_data': None,
        }])
        sb = _mock_sb(payments=payments)
        sb.rpc.return_value.execute.side_effect = [
            MagicMock(data=[{'provider_request_id': 'pr-old',
                             'is_reservation': False}]),
            MagicMock(data=[{'provider_request_id': 'pending:sentinel-1',
                             'is_reservation': True}]),
        ]
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-new', 'url': 'https://x',
                                 'status': 'pending'}) as mock_create, \
             patch('apps.payments.services.delete_payment_request',
                   side_effect=HitPayError('already completed', None, 409)
                   ) as mock_delete:
            response = api_client.post(
                '/payments/create',
                data={
                    'order_type': 'organizer_subscription',
                    'organizer_id': '33333333-3333-3333-3333-333333333333',
                    'amount': '99.00',
                    'email': 'org@example.com', 'name': 'Organizer',
                    'purpose': 'Organizer subscription',
                    'redirect_url': 'https://web.example.com/organizer/payment',
                },
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 201
        assert mock_create.call_args.args[0]['amount'] == '99.00'
        mock_delete.assert_called_once_with('pr-old')
        update_payloads = [c.args[0] for c in payments.update.call_args_list]
        assert {'status': 'expired'} in update_payloads

    def test_create_idempotent_equal_amount_reuse_preserved(self, api_client):
        """Price unchanged (149.00 = 149.00): the pending PR is reused as
        before — no release, no duplicate HitPay create."""
        payments = _table([{
            'provider_request_id': 'pr-old',
            'amount': '149.00',
            'status': 'pending',
            'qr_code_data': None,
        }])
        sb = _mock_sb(payments=payments)
        sb.rpc.return_value.execute.return_value = MagicMock(
            data=[{'provider_request_id': 'pr-old', 'is_reservation': False}])
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-old', 'url': 'https://x',
                                 'status': 'pending'}), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create',
                data={
                    'order_type': 'organizer_subscription',
                    'organizer_id': '33333333-3333-3333-3333-333333333333',
                    'amount': '149.00',
                    'email': 'org@example.com', 'name': 'Organizer',
                    'purpose': 'Organizer subscription',
                    'redirect_url': 'https://web.example.com/organizer/payment',
                },
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 200
        body = response.json()
        assert body['provider_request_id'] == 'pr-old'
        assert body['status'] == 'pending'
        mock_create.assert_not_called()
        payments.update.assert_not_called()


@pytest.mark.reliability
class TestSentinelRecovery:
    """in_progress-claim abandonment recovery (PR#23 CodeRabbit Major):
    adopt a live orphan PR, finalize a completed one, or — only for a
    verified pre-create crash — delete the sentinel and re-run the fresh
    flow once."""

    OLD_TS = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    def _recent_ts(self):
        # Computed at call time so slow CI (collection/imports between)
        # can't age a 'young' sentinel into recovery age (fixture flake).
        return datetime.now(UTC).isoformat()
    REG = '11111111-1111-1111-1111-111111111111'

    def _payload(self):
        return {
            'order_type': 'registration',
            'registration_id': self.REG,
            'amount': '50.00',
            'email': 'runner@example.com',
            'name': 'Runner One',
            'redirect_url': 'https://web.example.com/runner/payment?reg=x',
        }

    def _claim_in_progress_sb(self, payments, second_claim=None):
        """RPC: first claim is in_progress; an optional second result backs
        the recovery re-run's fresh claim."""
        sb = _mock_sb(registrations=_table([{'category_id': 'c1'}]),
                      race_categories=_table([{'price': '50.00'}]),
                      payments=payments)
        first = MagicMock(execute=MagicMock(return_value=MagicMock(
            data=[{'provider_request_id': None, 'is_reservation': False,
                   'in_progress': True}])))
        if second_claim is not None:
            second = MagicMock(execute=MagicMock(return_value=MagicMock(
                data=[second_claim])))
            sb.rpc = MagicMock(side_effect=[first, second])
        else:
            sb.rpc = MagicMock(return_value=first)
        return sb

    def _recovery_payments(self, sentinel_id='pending:stale-1',
                           created_at=None, linked_rows=None,
                           verify_rows=True):
        """Payments mock whose select chains come back in the recovery
        call order: 1 sentinel lookup, 2 linked-PR exclusion, 3
        adoption-verification."""
        payments = MagicMock()
        sentinel_chain, linked_chain, verify_chain = (MagicMock(),
                                                      MagicMock(),
                                                      MagicMock())
        sentinel_chain.eq.return_value.eq.return_value.eq.return_value \
            .like.return_value.limit.return_value.execute.return_value = \
            MagicMock(data=[{
                'provider_request_id': sentinel_id,
                'created_at': created_at or self.OLD_TS,
            }])
        linked_chain.eq.return_value.eq.return_value.not_.like.return_value \
            .execute.return_value = MagicMock(data=linked_rows or [])
        verify_chain.eq.return_value.limit.return_value.execute.return_value \
            = MagicMock(data=[{'provider_request_id': 'orphan-1'}]
                        if verify_rows else [])
        payments.select = MagicMock(
            side_effect=[sentinel_chain, linked_chain, verify_chain])
        return payments

    @staticmethod
    def _list_response(*prs):
        return list(prs)

    def test_recovery_no_candidates_deletes_sentinel_and_creates_fresh(
            self, api_client):
        payments = self._recovery_payments(verify_rows=False)
        sb = self._claim_in_progress_sb(
            payments, second_claim={
                'provider_request_id': 'pending:fresh-2',
                'is_reservation': True})
        hitpay_pr = {'id': 'pr-new', 'url': 'https://hp/pr-new',
                     'status': 'pending'}
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response()), \
             patch('apps.payments.services.create_payment_request',
                   return_value=hitpay_pr) as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 201
        # The stale sentinel was deleted (guarded: eq + like pending:%).
        delete_eqs = [c.args for c in
                      payments.delete.return_value.eq.call_args_list]
        assert ('provider_request_id', 'pending:stale-1') in delete_eqs
        delete_likes = payments.delete.return_value.eq.return_value.like \
            .call_args_list
        assert [c.args for c in delete_likes] == \
            [('provider_request_id', 'pending:%')]
        # HitPay create ran exactly once — on the fresh flow, after the
        # second claim.
        assert mock_create.call_count == 1
        payload = mock_create.call_args.args[0]
        assert payload['metadata[pelikat_order]'] == \
            f'registration:{self.REG}'
        # No secrets in the create payload.
        assert not any('secret' in k.lower() for k in payload)
        # The fresh sentinel was swapped for the real id.
        update = payments.update.call_args.args[0]
        assert update['provider_request_id'] == 'pr-new'

    def test_recovery_adopts_pending_orphan_reuses_url(self, api_client):
        orphan = {'id': 'orphan-1', 'url': 'https://hp/orphan',
                  'direct_link': {'direct_link_url': 'https://hp/dl'},
                  'status': 'pending',
                  'reference_number': self.REG,
                  'amount': '50.00', 'currency': 'MYR'}
        payments = self._recovery_payments()
        sb = self._claim_in_progress_sb(payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response(orphan)), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 200
        body = response.json()
        assert body == {
            'provider_request_id': 'orphan-1',
            'checkout_url': 'https://hp/orphan',
            'qr_code': None,
            'direct_link_url': 'https://hp/dl',
            'status': 'pending',
        }
        mock_create.assert_not_called()
        # Guarded single-condition adoption; update payload swaps the
        # sentinel for the orphan id.
        adopt = payments.update.call_args.args[0]
        assert adopt['provider_request_id'] == 'orphan-1'
        update_eqs = [c.args for c in
                      payments.update.return_value.eq.call_args_list]
        assert ('provider_request_id', 'pending:stale-1') in update_eqs

    def test_recovery_adopts_completed_orphan_and_confirms(self, api_client):
        orphan = {'id': 'orphan-2', 'url': 'https://hp/orphan2',
                  'status': 'completed',
                  'reference_number': self.REG,
                  'amount': '50.00', 'currency': 'MYR',
                  'payments': [{'id': 'pay-1', 'status': 'succeeded',
                                'payment_type': 'paynow'}]}
        payments = self._recovery_payments()
        sb = self._claim_in_progress_sb(payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response(orphan)), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 200
        assert response.json()['status'] == 'completed'
        mock_create.assert_not_called()
        # Same idempotent confirm_payment path the webhook uses.
        name, params = sb.rpc.call_args.args
        assert name == 'confirm_payment'
        assert params == {'p_provider_request_id': 'orphan-2',
                          'p_provider_payment_id': 'pay-1',
                          'p_rail': 'paynow'}
        # The adoption update never writes a non-pending status: the id is
        # persisted first, the rpc finalizes.
        adopt = payments.update.call_args.args[0]
        assert 'status' not in adopt

    def test_recovery_identity_mismatch_keeps_sentinel(self, api_client,
                                                        caplog):
        """A candidate that fails the strict identity check (wrong amount)
        is NEVER adopted or re-created past: deleting the sentinel would
        strand the REAL pending PR at HitPay — ops-leave-in-place 409."""
        orphan = {'id': 'orphan-9', 'url': 'https://hp/x', 'status': 'pending',
                  'reference_number': self.REG,
                  'amount': '99.99', 'currency': 'MYR'}
        payments = self._recovery_payments()
        sb = self._claim_in_progress_sb(payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response(orphan)), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 409
        assert response.json()['error'] == \
            'Payment initialization requires manual review; contact ' \
            'support or try a fresh registration later.'
        mock_create.assert_not_called()
        payments.delete.assert_not_called()
        payments.update.assert_not_called()
        assert any(('strict identity' in r.message and 'orphan-9' in r.message)
                   for r in caplog.records)

    def test_recovery_completed_confirm_failure_leaves_pending_row(
            self, api_client, caplog):
        """confirm_payment failing during completed-orphan adoption must
        NOT write a non-pending status: the row stays pending WITH the real
        PR id — exactly the scope reconcile_stale_payments polls
        (.eq('status', 'pending'), no sentinel filter) and the webhook's
        idempotent confirm matches."""
        orphan = {'id': 'orphan-2', 'url': 'https://hp/orphan2',
                  'status': 'completed',
                  'reference_number': self.REG,
                  'amount': '50.00', 'currency': 'MYR',
                  'payments': [{'id': 'pay-1', 'status': 'succeeded',
                                'payment_type': 'paynow'}]}
        payments = self._recovery_payments()
        sb = self._claim_in_progress_sb(payments)
        confirm = MagicMock(execute=MagicMock(
            side_effect=RuntimeError('rpc down')))
        sb.rpc = MagicMock(side_effect=[sb.rpc.return_value, confirm])
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response(orphan)), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 200
        body = response.json()
        assert body['status'] == 'pending'
        assert body['provider_request_id'] == 'orphan-2'
        assert 'confirmation is in progress' in body['message']
        mock_create.assert_not_called()
        # No finalized status was written: the adoption update carries only
        # the linkage fields (status stays 'pending' for reconcile proving
        # the real id is poll-targeted).
        adopt = payments.update.call_args.args[0]
        assert 'status' not in adopt
        assert adopt['provider_request_id'] == 'orphan-2'
        name, params = sb.rpc.call_args_list[-1].args
        assert name == 'confirm_payment'
        assert params['p_provider_request_id'] == 'orphan-2'
        assert any('confirm_payment failed' in r.message
                   for r in caplog.records)

    def test_recovery_unrelated_search_results_still_release_sentinel(
            self, api_client):
        """Partial-search noise that does NOT name the target is not a
        candidate: its presence does not block the delete+fresh-flow."""
        noise = {'id': 'other-order-pr', 'url': 'https://hp/other',
                 'status': 'pending', 'reference_number': '99999999',
                 'amount': '99.99', 'currency': 'MYR'}
        payments = self._recovery_payments(verify_rows=False)
        sb = self._claim_in_progress_sb(
            payments, second_claim={
                'provider_request_id': 'pending:fresh-2',
                'is_reservation': True})
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response(noise)), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-new', 'url': 'https://hp/n',
                                 'status': 'pending'}) as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 201
        assert mock_create.call_count == 1
        assert payments.update.call_args.args[0]['provider_request_id'] == 'pr-new'
        payments.delete.assert_called()

    def test_recovery_lookup_failure_keeps_sentinel(self, api_client):
        payments = self._recovery_payments()
        sb = self._claim_in_progress_sb(payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   side_effect=HitPayError('upstream exploded', status=503)), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 409
        assert response.json()['error'] == \
            'Checkout already initializing; retry in a few seconds'
        mock_create.assert_not_called()
        payments.delete.assert_not_called()
        payments.update.assert_not_called()

    def test_recovery_multiple_candidates_refuses_adoption(
            self, api_client, caplog):
        orphan_a = {'id': 'orphan-a', 'url': 'https://hp/a',
                    'status': 'pending', 'reference_number': self.REG,
                    'amount': '50.00', 'currency': 'MYR'}
        orphan_b = {'id': 'orphan-b', 'url': 'https://hp/b',
                    'status': 'pending', 'reference_number': self.REG,
                    'amount': '50.00', 'currency': 'MYR'}
        payments = self._recovery_payments()
        sb = self._claim_in_progress_sb(payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response(orphan_a, orphan_b)), \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 409
        mock_create.assert_not_called()
        payments.update.assert_not_called()
        payments.delete.assert_not_called()
        assert any('ambiguous' in r.message for r in caplog.records)

    def test_recovery_young_sentinel_not_deleted(self, api_client):
        """A seconds-old sentinel is a live in-flight create: never delete,
        never adopt — the plain 063 friendly 409 stands."""
        payments = self._recovery_payments(created_at=self._recent_ts())
        sb = self._claim_in_progress_sb(payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response()) as mock_list, \
             patch('apps.payments.services.create_payment_request') as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 409
        assert response.json()['error'] == \
            'Checkout already initializing; retry in a few seconds'
        # A young sentinel short-circuits before the HitPay lookup.
        mock_list.assert_not_called()
        mock_create.assert_not_called()
        payments.delete.assert_not_called()
        payments.update.assert_not_called()

    def test_recovery_id_already_linked_locally_is_excluded(self, api_client):
        """An orphan whose id already sits on a local row is not adopted —
        after exclusion the candidate list is empty, so the pre-create
        delete path (and its fresh flow) runs instead."""
        orphan = {'id': 'already-linked', 'url': 'https://hp/x',
                  'status': 'pending', 'reference_number': self.REG,
                  'amount': '50.00', 'currency': 'MYR'}
        payments = self._recovery_payments(
            linked_rows=[{'provider_request_id': 'already-linked'}],
            verify_rows=False)
        sb = self._claim_in_progress_sb(
            payments, second_claim={
                'provider_request_id': 'pending:fresh-2',
                'is_reservation': True})
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.list_payment_requests',
                   return_value=self._list_response(orphan)), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-new', 'url': 'https://hp/n',
                                 'status': 'pending'}) as mock_create:
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth())
        assert response.status_code == 201
        mock_create.assert_called_once()   # fresh flow, no adopt


@pytest.mark.reliability
class TestPaymentStatus:
    def test_status_returns_fields(self, api_client):
        payments = _table([{'status': 'pending', 'payment_rail': None,
                            'provider_request_id': 'pr-9'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)):
            response = api_client.get(
                '/payments/status?reference_number=reg-1', **_auth()
            )
        assert response.status_code == 200
        body = response.json()
        assert body['status'] == 'pending'
        assert body['provider_request_id'] == 'pr-9'
        assert body['payment_rail'] is None

    def test_status_requires_reference(self, api_client):
        response = api_client.get('/payments/status', **_auth())
        assert response.status_code == 400

    def test_status_unknown_reference_404(self, api_client):
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=_table([]))):
            response = api_client.get(
                '/payments/status?reference_number=nope', **_auth()
            )
        assert response.status_code == 404


@pytest.mark.reliability
class TestMerchFinalizeSelfHeal:
    """CodeRabbit PR#17 (thread PRRT_kwDOSKXhgc6yOekg): if
    merch_confirm_payment fails after confirm_payment succeeded, the
    payment row is completed and the webhook (one-shot), status-poll
    pending branch and reconcile (pending rows only) all skip it — the
    merch order would stay pending forever. A completed row must
    re-run the idempotent finalize on the next status poll."""

    def _merch_row(self, **overrides):
        row = {'status': 'completed', 'payment_rail': None,
               'provider_request_id': 'pr-m-1',
               'order_type': 'merch', 'merch_order_id': 'mo-21'}
        row.update(overrides)
        return row

    def _webhook_body(self):
        return json.dumps({
            'id': 'pr-m-1',
            'object': 'payment_request',
            'status': 'completed',
            'reference_number': 'mo-21',
            'payments': [{
                'id': 'pay-m-1',
                'status': 'succeeded',
                'amount': '49.00',
                'payment_type': 'fpx',
            }],
        }).encode()

    def _sign(self, body: bytes) -> str:
        return hmac.new(WEBHOOK_SALT.encode(), body, hashlib.sha256) \
            .hexdigest()

    def _merch_finalize_calls(self, sb):
        return [c for c in sb.rpc.call_args_list
                if c.args[0] == 'merch_confirm_payment']

    def test_webhook_survives_finalize_failure_and_status_poll_heals(self):
        payments = _table([self._merch_row()])
        sb = _mock_sb(payments=payments)
        body = self._webhook_body()

        # Attempt 1: confirm_payment (rpc) succeeds at the DB, but the
        # merch finalize raises — the webhook must still answer 200 and
        # the order must stay pending (no merch_confirm_payment call).
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch.object(services, '_finalize_merch_order',
                          side_effect=RuntimeError('db hiccup')) as forced:
            status, resp_body = services.handle_webhook(
                body, self._sign(body), 'payment_request.completed')
        assert status == 200
        assert resp_body == {'status': 'handler_error'}
        forced.assert_called_once()
        assert self._merch_finalize_calls(sb) == []  # order still pending

        # Attempt 2 (self-heal): the next status poll for the SAME PR
        # reference re-runs the idempotent merch finalize — no 500, and
        # the rpc gets the exact confirm args it needs.
        with patch('apps.payments.services.get_sb', return_value=sb):
            status, body = services.get_payment_status('mo-21')
        assert status == 200
        assert body['status'] == 'completed'
        assert body['provider_request_id'] == 'pr-m-1'
        finalize_calls = self._merch_finalize_calls(sb)
        assert len(finalize_calls) == 1
        assert finalize_calls[0].args[1] == {
            'p_order_id': 'mo-21',
            'p_payment_ref': 'pr-m-1',
            'p_payment_method': None,  # keep the checkout-time method
        }

        # Attempt 3 (idempotence): a repeat status poll re-runs the rpc
        # with identical args (062 same-payment_ref idempotency) and
        # stays a 200 completed read — never a 500, no state change.
        with patch('apps.payments.services.get_sb', return_value=sb):
            status, body = services.get_payment_status('mo-21')
        assert status == 200
        assert body['status'] == 'completed'
        finalize_calls = self._merch_finalize_calls(sb)
        assert len(finalize_calls) == 2
        assert finalize_calls[1].args[1] == finalize_calls[0].args[1]

    def test_webhook_rpc_failure_answered_ok_and_status_poll_heals_via_helper(self):
        """Companion to the forced-finalize test above: there the helper
        is replaced, so its try/except never runs. Here the REAL
        _finalize_merch_order runs and the merch_confirm_payment rpc
        raises at its actual call site — the webhook must still answer
        'ok' (the helper swallows it, no handler_error), the payment row
        stays completed, and the next status poll retries the rpc
        through the very same helper."""

        payments = _table([self._merch_row()])
        sb = _mock_sb(payments=payments)

        def _rpc_raiser(name, payload=None):
            if name == 'merch_confirm_payment':
                raise RuntimeError('merch rpc down')
            return MagicMock()  # confirm_payment etc. behave normally

        sb.rpc.side_effect = _rpc_raiser
        body = self._webhook_body()
        with patch('apps.payments.services.get_sb', return_value=sb):
            status, resp_body = services.handle_webhook(
                body, self._sign(body), 'payment_request.completed')
        assert status == 200
        assert resp_body == {'status': 'ok'}  # helper swallowed the failure
        assert len(self._merch_finalize_calls(sb)) == 1  # attempted once

        # Self-heal: same mock, rpc healthy again — the completed row's
        # status-poll finalize retry runs the real helper end-to-end.
        sb.rpc.side_effect = None
        with patch('apps.payments.services.get_sb', return_value=sb):
            status, body = services.get_payment_status('mo-21')
        assert status == 200
        assert body['status'] == 'completed'
        finalize_calls = self._merch_finalize_calls(sb)
        assert len(finalize_calls) == 2  # rpc retried through the helper
        assert finalize_calls[1].args[1] == {
            'p_order_id': 'mo-21',
            'p_payment_ref': 'pr-m-1',
            'p_payment_method': None,
        }

    def test_completed_non_merch_row_finalize_is_a_no_op_select(self):
        """A completed registration row surfaced by the status read must
        not trigger any merch rpc (the finalize's row lookup filters on
        order_type='merch' + merch_order_id)."""
        payments = _table([{'status': 'completed', 'payment_rail': 'fpx',
                            'provider_request_id': 'pr-9',
                            'order_type': 'registration'}])
        sb = _mock_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb):
            status, body = services.get_payment_status('reg-1')
        assert status == 200
        assert body['status'] == 'completed'
        assert self._merch_finalize_calls(sb) == []
        assert sb.rpc.call_args_list == []  # nothing re-confirmed


@pytest.mark.reliability
class TestStatusPollHitPayThrottle:
    """CodeRabbit PR#17 (thread PRRT_kwDOSKXhgc6yOeki): the pending
    branch of get_payment_status must not call HitPay synchronously on
    every read (up to 20s each; concurrent polls can starve all gunicorn
    workers). Throttle by provider_request_id: skip when the PR was
    polled within the last few seconds and report the last known state."""

    @pytest.fixture(autouse=True)
    def _reset_throttle(self):
        services._STATUS_POLL_LAST_CHECK.clear()
        yield
        services._STATUS_POLL_LAST_CHECK.clear()

    def _pending_status(self):
        payments = _table([{'status': 'pending', 'payment_rail': None,
                            'provider_request_id': 'pr-9'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)):
            return services.get_payment_status('reg-1')

    def test_remote_call_skipped_on_immediate_second_check(self):
        with patch('apps.payments.services.get_payment_request',
                   return_value={'status': 'pending'}) as remote:
            status, body = self._pending_status()
            assert status == 200
            assert body['status'] == 'pending'
            assert remote.call_count == 1  # first read polls HitPay

            status, body = self._pending_status()
            assert status == 200
            assert body == {'status': 'pending', 'payment_rail': None,
                            'provider_request_id': 'pr-9'}
            assert remote.call_count == 1  # throttled: no second HitPay call

    def test_remote_call_executed_again_after_window(self):
        with patch('apps.payments.services.get_payment_request',
                   return_value={'status': 'pending'}) as remote:
            status, body = self._pending_status()
            assert status == 200
            assert remote.call_count == 1

            # Age the recorded attempt past the throttle window.
            services._STATUS_POLL_LAST_CHECK['pr-9'] = (
                time.monotonic() - services.STATUS_POLL_THROTTLE_SECONDS - 1)

            status, body = self._pending_status()
            assert status == 200
            assert body['status'] == 'pending'
            assert remote.call_count == 2  # window elapsed: polled again

    def _slow_remote(self, pr_id):
        """Simulate a ~20s gateway slowdown: by the time the remote call
        RETURNS the recorded attempt-start is already >3s old."""
        services._STATUS_POLL_LAST_CHECK[pr_id] = \
            time.monotonic() - services.STATUS_POLL_THROTTLE_SECONDS - 17
        return {'status': 'pending'}

    def test_slow_remote_call_rearms_cooldown_on_completion(self):
        """The cooldown re-arms from COMPLETION time (not just the call
        start), so a slow HitPay call does not free the very next read —
        the worker still reports the stored status instead of polling."""
        with patch('apps.payments.services.get_payment_request',
                   side_effect=self._slow_remote) as remote:
            status, body = self._pending_status()  # call "takes 20s"
            assert status == 200
            assert body['status'] == 'pending'
            # Cooldown re-armed at completion (call finished moments ago).
            assert services._STATUS_POLL_LAST_CHECK['pr-9'] > (
                time.monotonic() - services.STATUS_POLL_THROTTLE_SECONDS)

            status, body = self._pending_status()
            assert status == 200
            assert body['status'] == 'pending'
            assert remote.call_count == 1  # throttled after the slow call


@pytest.mark.reliability
class TestRefund:
    @classmethod
    def _completed_rows(cls, rail):
        return {
            'provider_request_id': 'pr-9',
            'provider_payment_id': 'pay-1',
            'payment_rail': rail,
            'status': 'completed',
            'amount': '10.00',
            'registration_id': 'reg-1',
        }

    def _completed_payment(self, rail):
        return _table([self._completed_rows(rail)])

    def test_refund_blocks_nonrefundable_rails(self, api_client):
        """duitnow refunds must be refused server-side and flagged."""
        payments = self._completed_payment('duitnow')
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund') as mock_refund:
            response = api_client.post(
                '/payments/refund',
                data={'provider_request_id': 'pr-9'},
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 422
        assert response.json()['refund_blocked_reason'] == 'manual_disbursement'
        mock_refund.assert_not_called()
        update = payments.update.call_args.args[0]
        assert update['refund_blocked_reason'] == 'manual_disbursement'

    def test_refund_success_for_refundable_rail(self, api_client):
        payments = self._completed_payment('fpx')
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund',
                   return_value={'id': 'r-1', 'status': 'succeeded'}) as mock_refund:
            response = api_client.post(
                '/payments/refund',
                data={'provider_request_id': 'pr-9'},
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 200
        mock_refund.assert_called_once_with('pay-1', '10.00')
        assert payments.update.call_args.args[0]['status'] == 'refunded'

    def test_refund_unknown_payment_404(self, api_client):
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=_table([]))):
            response = api_client.post(
                '/payments/refund',
                data={'provider_request_id': 'nope'},
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 404

    def test_refund_without_charge_reference_409(self, api_client):
        """M2: no provider_payment_id → no HitPay refund call possible."""
        rows = [{
            'provider_request_id': 'pr-9',
            'provider_payment_id': None,
            'payment_rail': 'fpx',
            'status': 'completed',
            'amount': '10.00',
            'registration_id': 'reg-1',
        }]
        payments = _table(rows)
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund') as mock_refund:
            response = api_client.post(
                '/payments/refund',
                data={'provider_request_id': 'pr-9'},
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 409
        assert 'no charge reference' in response.json()['error']
        mock_refund.assert_not_called()

    def test_refund_rejects_invalid_amounts(self, api_client):
        """M3: client amount must be numeric, > 0, ≤ stored amount.
        '-1'/'0'/'abc' die at pre-validation; '99.00' is valid input that
        dies at the post-claim remaining-bound check — so the claim must
        succeed here (real payment rows, never a bare MagicMock)."""
        rows = self._completed_rows('fpx')
        for bad_amount in ('-1', '0', 'abc', '99.00'):
            payments = _table([rows])
            payments.update.return_value.eq.return_value.in_.return_value \
                .is_.return_value.execute.return_value = MagicMock(data=[{'id': 1}])
            with patch('apps.payments.services.get_sb',
                       return_value=_mock_sb(payments=payments)), \
                 patch('apps.payments.services.create_refund') as mock_refund:
                response = api_client.post(
                    '/payments/refund',
                    data={'provider_request_id': 'pr-9', 'amount': bad_amount},
                    content_type='application/json', **_auth(),
                )
            assert response.status_code == 422, bad_amount
            assert response.json()['error'].startswith('Refund amount'), bad_amount
            if bad_amount == '99.00':
                # Above the remaining amount → survived pre-validation,
                # claimed the slot, and was refused by the remaining check.
                assert 'remaining' in response.json()['error'], bad_amount
                assert payments.update.call_count >= 2, bad_amount  # claim + release
            mock_refund.assert_not_called()

    def test_refund_hitpay_failure_releases_claim_502(self):
        """H2: if HitPay rejects the refund, the in-flight marker is cleared."""
        payments = self._completed_payment('fpx')
        # Claim update succeeds (first call).
        claim_resp = MagicMock(data=[{'id': 1}])
        payments.update.return_value.eq.return_value.in_.return_value \
            .is_.return_value.execute.return_value = claim_resp
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund',
                   side_effect=HitPayError('boom', status=400)):
            status, _body = services.refund_payment('pr-9', None)
        assert status == 502
        # Claim released.
        release = payments.update.call_args.args[0]
        assert release.get('refund_blocked_reason') is None

    def test_refund_double_call_second_request_409(self):
        """H2: only one caller can claim the payment for refund."""
        payments = self._completed_payment('fpx')
        claim_exec = MagicMock(side_effect=[
            MagicMock(data=[{'id': 1}]),   # first request wins the claim
            MagicMock(data=[]),            # second request finds it claimed
        ])
        payments.update.return_value.eq.return_value.in_.return_value \
            .is_.return_value.execute = claim_exec
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund',
                   return_value={'id': 'r-1'}):
            first = services.refund_payment('pr-9', None)
            second = services.refund_payment('pr-9', None)
        assert first[0] == 200
        assert first[1]['status'] == 'refunded'
        assert second[0] == 409
        assert 'already refunded or refund' in second[1]['error']


@pytest.mark.reliability
class TestReconcileCommand:
    def _stale_pending(self):
        return _table([{
            'provider_request_id': 'pr-9',
            'registration_id': 'reg-1',
            'organizer_id': None,
            'status': 'pending',
            'created_at': '2026-10-17T00:00:00Z',
        }])

    def test_reconcile_completed_runs_confirm_path(self):
        payments = self._stale_pending()
        sb = _mock_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'completed',
                                 'payments': [{'id': 'pay-1', 'status': 'succeeded',
                                               'payment_type': 'fpx'}]}):
            call_command('reconcile_payments')
        sb.rpc.assert_called_once_with(
            'confirm_payment',
            {'p_provider_request_id': 'pr-9',
             'p_provider_payment_id': 'pay-1',
             'p_rail': 'fpx'},
        )

    def test_reconcile_expired_recorded(self):
        payments = self._stale_pending()
        sb = _mock_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'expired'}):
            call_command('reconcile_payments')
        sb.rpc.assert_not_called()
        assert payments.update.call_args.args[0]['status'] == 'expired'

    def test_reconcile_poll_error_does_not_crash(self):
        payments = self._stale_pending()
        sb = _mock_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   side_effect=Exception('hitpay down')):
            call_command('reconcile_payments')
        sb.rpc.assert_not_called()

    def test_reconcile_overlapping_run_does_not_double_poll(self):
        """L2: overlapping cron runs claim stale rows via the webhook_events
        payload_hash dedupe — the loser must not re-poll or re-audit."""
        payments = self._stale_pending()
        events = _table()
        events.insert.return_value.execute = MagicMock(side_effect=[
            MagicMock(data=[]),   # first run claims the row
            Exception('duplicate key value violates unique constraint '
                      '"webhook_events_payload_hash_key"'),
        ])
        sb = _mock_sb(payments=payments, webhook_events=events)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'expired'}) as mock_get:
            call_command('reconcile_payments')
            call_command('reconcile_payments')
        assert mock_get.call_count == 1
        assert payments.update.call_count == 1  # no re-record on the 2nd run


@pytest.mark.reliability
class TestReconcileMarkerRelease:
    def _stale_pending(self):
        return _table([{
            'provider_request_id': 'pr-9',
            'registration_id': 'reg-1',
            'organizer_id': None,
            'status': 'pending',
            'created_at': '2026-10-17T00:00:00Z',
        }])

    def test_completed_releases_no_charge_reference_marker(self):
        """W2: on a completed poll (charge reference now known), a stuck
        'no_charge_reference' marker must be cleared so refunds un-409."""
        payments = self._stale_pending()
        sb = _mock_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'completed',
                                 'payments': [{'id': 'pay-1', 'status': 'succeeded',
                                               'payment_type': 'fpx'}]}):
            call_command('reconcile_payments')
        marker_clears = [
            c for c in payments.update.call_args_list
            if c.args[0].get('refund_blocked_reason') is None
        ]
        assert len(marker_clears) == 1
        # Guarded: only the no_charge_reference marker may be cleared. The
        # clear chain is update(...).eq(provider_request_id).eq(refund_...).
        chain = payments.update.return_value.eq.return_value
        eq_guard = [c.args for c in chain.eq.call_args_list]
        assert ('refund_blocked_reason', 'no_charge_reference') in eq_guard
        eqs = [c.args for c in payments.update.return_value.eq.call_args_list]
        # still targeted at the right payment row
        assert ('provider_request_id', 'pr-9') in eqs

    def test_manual_disbursement_marker_not_cleared(self):
        """W2: an H2 'manual_disbursement' marker must never be released."""
        payments = self._stale_pending()
        sb = _mock_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'completed',
                                 'payments': [{'id': 'pay-1', 'status': 'succeeded',
                                               'payment_type': 'fpx'}]}):
            call_command('reconcile_payments')
        # No clearing update without the no_charge_reference guard: every
        # refund_blocked_reason clearing update must be eq-guarded.
        for c in payments.update.call_args_list:
            if c.args[0].get('refund_blocked_reason') is None:
                guard = [
                    e.args for e in
                    payments.update.return_value.eq.return_value.eq.call_args_list
                ]
                assert ('refund_blocked_reason', 'no_charge_reference') in guard


@pytest.mark.reliability
class TestReconcileClaimLifecycle:
    """L2: claims must release on success and reclaim after a 10-minute TTL —
    but only 'reconcile.poll' claim rows, never real webhook events."""

    def test_success_releases_claim_and_next_run_can_repoll(self):
        payments = _table([{
            'provider_request_id': 'pr-9', 'registration_id': 'reg-1',
            'organizer_id': None, 'status': 'pending',
            'created_at': '2026-10-17T00:00:00Z',
        }])
        events = _table()
        events.insert.return_value.execute = MagicMock(
            side_effect=[MagicMock(data=[]), MagicMock(data=[])])
        sb = _mock_sb(payments=payments, webhook_events=events)
        pr = {'id': 'pr-9', 'status': 'expired'}
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value=pr) as mock_get:
            call_command('reconcile_payments')
            call_command('reconcile_payments')
        assert mock_get.call_count == 2
        eqs = [c.args for c in events.delete.return_value.eq.call_args_list]
        claim_hash = hashlib.sha256(b'reconcile:pr-9').hexdigest()
        assert eqs.count(('payload_hash', claim_hash)) >= 1

    def test_stale_claim_reclaimed_after_ten_minutes(self):
        events = _table()
        events.insert.return_value.execute = MagicMock(
            side_effect=Exception('duplicate key value violates unique '
                                  'constraint "webhook_events_payload_hash_key"'))
        events.select.return_value.eq.return_value.maybe_single \
            .return_value.execute.return_value = MagicMock(data={
                'received_at': (datetime.now(UTC)
                                - timedelta(minutes=20)).isoformat()})
        payments = _table([{
            'provider_request_id': 'pr-9', 'registration_id': 'reg-1',
            'organizer_id': None, 'status': 'pending',
            'created_at': '2026-10-17T00:00:00Z',
        }])
        sb = _mock_sb(payments=payments, webhook_events=events)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'expired'}):
            call_command('reconcile_payments')
        eqs = [c.args for c in events.delete.return_value.eq.call_args_list]
        claim_hash = hashlib.sha256(b'reconcile:pr-9').hexdigest()
        assert ('payload_hash', claim_hash) in eqs  # orphaned claim removed
        assert payments.update.call_count == 1  # row was re-polled + recorded

    def test_fresh_claim_still_skipped(self):
        """The spec'd supabase-py probe chain is wired onto the mock
        Supabase's webhook_events table and passes through
        _reconcile_claim_orphaned: a fresh claim is skipped this run."""
        events = MagicMock()
        events.insert.return_value.execute = MagicMock(
            side_effect=Exception('duplicate key value violates unique '
                                  'constraint "webhook_events_payload_hash_key"'))
        probe = events.select.return_value.eq.return_value = MagicMock(
            spec=['maybe_single'])
        probe.maybe_single.return_value.execute.return_value = MagicMock(data={
            'received_at': (datetime.now(UTC)
                            - timedelta(minutes=1)).isoformat()})
        payments = _table([{
            'provider_request_id': 'pr-9', 'registration_id': 'reg-1',
            'organizer_id': None, 'status': 'pending',
            'created_at': '2026-10-17T00:00:00Z',
        }])
        sb = _mock_sb(payments=payments, webhook_events=events)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'expired'}):
            call_command('reconcile_payments')
        assert probe.maybe_single.called  # wired chain is exercised
        assert payments.update.call_count == 0  # skipped this run
        assert events.delete.return_value.eq.call_count == 0

    def test_claim_queries_are_scoped_to_reconcile_hash(self):
        """Real webhook rows must never be TTL-deleted: every webhook_events
        delete from reconcile is scoped by the exact claim payload_hash."""
        events = _table()
        events.insert.return_value.execute = MagicMock(
            side_effect=Exception('duplicate key value violates unique '
                                  'constraint "webhook_events_payload_hash_key"'))
        events.select.return_value.eq.return_value.maybe_single \
            .return_value.execute.return_value = MagicMock(data={
                'received_at': (datetime.now(UTC)
                                - timedelta(minutes=20)).isoformat()})
        payments = _table([{
            'provider_request_id': 'pr-9', 'registration_id': 'reg-1',
            'organizer_id': None, 'status': 'pending',
            'created_at': '2026-10-17T00:00:00Z',
        }])
        sb = _mock_sb(payments=payments, webhook_events=events)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.services.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'expired'}):
            call_command('reconcile_payments')
        claim_hash = hashlib.sha256(b'reconcile:pr-9').hexdigest()
        for c in events.delete.return_value.eq.call_args_list:
            assert c.args == ('payload_hash', claim_hash)
        # the staleness lookup is hash-scoped too, not event_type-wide
        assert events.select.return_value.eq.call_args_list[-1].args == \
            ('payload_hash', claim_hash)


@pytest.mark.reliability
class TestReconcileClaimProbe:
    """Pin the supabase-py method-name bug class: the claim-staleness probe
    must use `maybe_single()` (supabase-py) — NOT `maybeSingle()` (the JS
    name, which AttributeError-crashed the handler and killed reconcile on
    ANY claim conflict). The probe chain is wired onto the mock Supabase's
    webhook_events table and exercised through `_reconcile_claim_orphaned`,
    so `maybe_single` being actually CALLED is asserted functionally."""

    def _spec_events(self):
        # spec_set-style: only the real supabase-py chain exists on the
        # probe node; maybeSingle would raise AttributeError instead of
        # silently passing.
        t = MagicMock()
        t.select.return_value.eq.return_value = MagicMock(
            spec=['maybe_single'])
        return t

    def _wired_sb(self, execute_return=None):
        """Spec'd supabase-py probe chain wired onto the mock Supabase's
        webhook_events table (used through reconcile / _reconcile_claim_orphaned).
        `execute_return` is whatever the probe's execute() yields."""
        events = self._spec_events()
        probe = events.select.return_value.eq.return_value
        probe.maybe_single.return_value.execute.return_value = execute_return
        return _mock_sb(webhook_events=events), events, probe

    def test_chain_uses_supabase_py_maybe_single(self):
        """Functional pin: the wired probe chain actually calls
        supabase-py's maybe_single() (never the JS-name maybeSingle)."""
        sb, _events, probe = self._wired_sb(MagicMock(data=None))
        from apps.payments.services import _reconcile_claim_orphaned
        assert _reconcile_claim_orphaned(sb, 'hash-x') is False
        assert probe.maybe_single.called
        with pytest.raises(AttributeError):
            probe.maybeSingle  # noqa: B018

    def test_maybe_single_null_row_is_not_orphaned(self):
        """A None row (claim absent) is conservatively NOT orphaned."""
        sb, _, _ = self._wired_sb(MagicMock(data=None))
        from apps.payments.services import _reconcile_claim_orphaned
        assert _reconcile_claim_orphaned(sb, 'hash-x') is False

    def test_maybe_single_none_result_is_not_orphaned(self):
        """execute() returning None → treated as empty/unknown age → the
        conservative no-op (decline allegation), no crash."""
        sb, _, probe = self._wired_sb(None)
        from apps.payments.services import _reconcile_claim_orphaned
        assert _reconcile_claim_orphaned(sb, 'hash-x') is False
        assert probe.maybe_single.called



@pytest.mark.reliability
class TestRefundRemainingAndUnknown:
    """062 hardening: cumulative refunded_amount + remaining validation; and
    unknown-outcome refunds keep their claim under the refund_unknown marker."""

    def _row(self, **over):
        row = {
            'provider_request_id': 'pr-9',
            'provider_payment_id': 'pay-1',
            'payment_rail': 'fpx',
            'status': 'completed',
            'amount': '10.00',
            'refunded_amount': '0',
            'registration_id': 'reg-1',
        }
        row.update(over)
        return row

    def _payments(self, row, claim=True):
        t = _table([row])
        if claim:
            t.update.return_value.eq.return_value.in_.return_value \
                .is_.return_value.execute.return_value = MagicMock(
                    data=[{'id': 1}])
        return t

    def test_refund_partial_updates_cumulative_refunded_amount(self):
        payments = self._payments(self._row(refunded_amount='4.00'))
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund',
                   return_value={'id': 'r-1', 'status': 'succeeded'}) as mock_refund:
            status, _ = services.refund_payment('pr-9', '3.00')
        assert status == 200
        mock_refund.assert_called_once_with('pay-1', '3.00')
        update = payments.update.call_args.args[0]
        assert update['status'] == 'partially_refunded'
        assert update['refunded_amount'] == '7.00'
        assert update['refund_blocked_reason'] is None

    def test_refund_default_amount_is_remaining(self):
        payments = self._payments(self._row(refunded_amount='4.00'))
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund',
                   return_value={'id': 'r-1', 'status': 'succeeded'}):
            status, _ = services.refund_payment('pr-9', None)
        assert status == 200
        assert payments.update.call_args.args[0]['status'] == 'refunded'
        assert payments.update.call_args.args[0]['refunded_amount'] == '10.00'

    def test_refund_over_remaining_422(self):
        """Post-claim validation: the fresh refunded_amount is authoritative.
        The claim is won, then released again after the over-refund 422."""
        payments = self._payments(self._row(refunded_amount='6.00'))
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund') as mock_refund:
            status, body = services.refund_payment('pr-9', '5.00')
        assert status == 422
        assert 'remaining' in body['error']
        mock_refund.assert_not_called()
        # Claim cleared again (guarded inverse of the claim update).
        updates = [c.args[0] for c in payments.update.call_args_list]
        assert {'refund_blocked_reason': None} in updates

    def test_refund_post_claim_validation_reads_fresh_refunded_amount(self):
        """Refund race: a concurrent winner that bumped refunded_amount between
        our initial read and our claim must not be double-charged — the
        post-claim fresh read is validated, so the second refund 422s instead
        of forwarding to HitPay (simulated concurrency: sequential calls whose
        select chain returns the freshly-updated row)."""
        row_first = self._row(refunded_amount='0')
        row_after = self._row(refunded_amount='10.00')
        payments = _table([row_first])
        select_exec = MagicMock(side_effect=[
            MagicMock(data=[row_first]),   # call 1: initial read
            MagicMock(data=[row_first]),   # call 1: post-claim fresh read
            MagicMock(data=[row_after]),   # call 2: initial read (fresh row)
            MagicMock(data=[row_after]),   # call 2: post-claim fresh read
        ])
        payments.select.return_value.eq.return_value.limit.return_value.execute \
            = select_exec
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund',
                   return_value={'id': 'r-1'}) as mock_refund:
            first = services.refund_payment('pr-9', '10.00')
            second = services.refund_payment('pr-9', '5.00')
        assert first[0] == 200
        assert second[0] == 422
        assert 'remaining' in second[1]['error']
        assert mock_refund.call_count == 1
        # HitPay was only ever invoked with the first claim's validated amount
        mock_refund.assert_called_once_with('pay-1', '10.00')
        # the first refund's cumulative write and the second's claim release
        updates = [c.args[0] for c in payments.update.call_args_list]
        assert [u.get('refunded_amount') for u in updates
                if 'refunded_amount' in u] == ['10.00']
        # Every state transition was a guarded update, not a bare overwrite:
        # claim(winner) + claim(loser) + release must all have filtered on
        # the same payments row (claim + release ≥ 2 guarded chains).
        assert len(payments.update.return_value.eq.call_args_list) >= 2
        # The release itself is exactly the marker clear — no co-writes.
        assert updates[-1] == {'refund_blocked_reason': None}

    def test_refund_claim_released_on_post_claim_validation_failure(self):
        """A post-claim validation failure clears the refund_in_progress
        marker so the slot stays refundable for a corrected retry."""
        payments = self._payments(self._row(refunded_amount='10.00'))
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund') as mock_refund:
            status, _ = services.refund_payment('pr-9', '5.00')
        assert status == 422
        mock_refund.assert_not_called()
        chains = [c.args for c in
                  payments.update.return_value.eq.call_args_list
                  if c.args == ('provider_request_id', 'pr-9')]
        assert chains  # the release targeted the claimed row

    def test_refund_unknown_outcome_keeps_claim_with_distinct_marker(self):
        """Timeout/transport/5xx: claim kept under 'refund_unknown' —
        reconciliation resolves truth; never auto-refundable."""
        for exc in (Exception('read timeout'),
                    HitPayError('upstream exploded', status=503)):
            payments = self._payments(self._row())
            with patch('apps.payments.services.get_sb',
                       return_value=_mock_sb(payments=payments)), \
                 patch('apps.payments.services.create_refund', side_effect=exc):
                status, body = services.refund_payment('pr-9', None)
            assert status == 502
            assert body == {'error': 'Refund failed at HitPay'}
            update = payments.update.call_args.args[0]
            assert update['refund_blocked_reason'] == 'refund_unknown'

    def test_refund_hitpay_4xx_releases_claim(self):
        payments = self._payments(self._row())
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund',
                   side_effect=HitPayError('bad id', status=400)):
            status, body = services.refund_payment('pr-9', None)
        assert status == 502
        # stable generic body: no exception detail leaks (CodeQL GAS 12)
        assert body == {'error': 'Refund failed at HitPay'}
        update = payments.update.call_args.args[0]
        assert update.get('refund_blocked_reason') is None

    def test_refund_completed_without_charge_reference_writes_no_marker(self):
        """W2: a completed-but-missing-provider-id row stays claimable —
        no refund-block marker is ever persisted for it."""
        payments = self._payments(self._row(provider_payment_id=None),
                                  claim=False)
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund') as mock_refund:
            status, _ = services.refund_payment('pr-9', None)
        assert status == 409
        mock_refund.assert_not_called()
        payments.update.assert_not_called()

    def test_refund_amount_validation_failure_never_claims(self):
        payments = self._payments(self._row())
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
             patch('apps.payments.services.create_refund') as mock_refund:
            status, _ = services.refund_payment('pr-9', 'abc')
        assert status == 422
        mock_refund.assert_not_called()
        payments.update.assert_not_called()


@pytest.mark.reliability
class TestLateWebhookOnExpiredRow:
    """F2: a row locally set 'expired' (amount-mismatch release) can still
    be paid through its old checkout URL before the remote PR is cancelled.
    The customer paid a price they saw, so a late completed webhook must
    finalize the row: re-arm 'expired' → 'pending' (guarded, no double
    audit) and let confirm_payment complete it."""

    def _payload_body(self, amount='149.00'):
        return json.dumps({
            'id': 'pr-expired',
            'object': 'payment_request',
            'status': 'completed',
            'reference_number': 'ref-1',
            'payments': [{
                'id': 'pay-1',
                'status': 'succeeded',
                'amount': amount,
                'payment_type': 'grabpay_direct',
            }],
        }).encode()

    def _expired_row(self, **overrides):
        row = {'id': 'p-1', 'provider_request_id': 'pr-expired',
               'status': 'expired', 'amount': '149.00'}
        row.update(overrides)
        return row

    def _expired_payments_table(self, row=None):
        t = MagicMock()
        t.select.return_value.eq.return_value.limit.return_value.execute\
            .return_value = MagicMock(data=[row if row is not None
                                            else self._expired_row()])
        t.update.return_value.eq.return_value.execute.return_value = \
            MagicMock(data=[])
        t.insert.return_value.execute.return_value = MagicMock(data=[])
        return t

    def _sign(self, body: bytes) -> str:
        return hmac.new(WEBHOOK_SALT.encode(), body,
                        hashlib.sha256).hexdigest()

    def _post(self, sb):
        body = self._payload_body()
        with patch('apps.payments.services.get_sb', return_value=sb):
            return services.handle_webhook(
                body, self._sign(body), 'payment_request.completed')

    def test_expired_row_is_rearmed_and_confirmed(self):
        payments = self._expired_payments_table()
        sb = _mock_sb(payments=payments)
        status, _ = self._post(sb)
        assert status == 200
        # Guarded re-arm: expired → pending, scoped to still-expired rows.
        update_payloads = [c.args[0]
                           for c in payments.update.call_args_list]
        assert {'status': 'pending'} in update_payloads
        eqs = ([c.args for c in payments.update.return_value.eq.call_args_list] +
               [c.args for c in payments.update.return_value.eq.return_value.eq
                .call_args_list])
        assert ('status', 'expired') in eqs
        assert ('provider_request_id', 'pr-expired') in eqs
        # confirm_payment still runs — it owns the completion and the audit.
        sb.rpc.assert_called_once()
        assert sb.rpc.call_args.args[0] == 'confirm_payment'
        assert sb.rpc.call_args.args[1]['p_provider_request_id'] == 'pr-expired'

    def test_pending_row_is_not_double_rearmed(self):
        """Only locally-expired rows get re-armed; a still-pending row goes
        straight to confirm_payment with no extra update."""
        payments = self._expired_payments_table(row=self._expired_row(
            status='pending'))
        sb = _mock_sb(payments=payments)
        status, _ = self._post(sb)
        assert status == 200
        assert payments.update.call_args_list == []
        assert sb.rpc.call_args.args[0] == 'confirm_payment'

    def test_no_extra_audit_beyond_confirm_payment(self):
        """The handler must not double-write audit: the RPC is the sole
        audit writer for the confirm."""
        payments = self._expired_payments_table()
        audit_log = _table()
        sb = _mock_sb(payments=payments, audit_log=audit_log)
        self._post(sb)
        assert audit_log.insert.call_args_list == []
