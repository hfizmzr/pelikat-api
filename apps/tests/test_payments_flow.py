"""
HitPay payments flow tests: create / status / refund / reconcile,
plus internal-key middleware behaviour for the /payments/* surface.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import Client

from apps.payments import services
from apps.payments.hitpay import HitPayError

WEBHOOK_PATH = '/payments/webhook/hitpay/'
WEBHOOK_SALT = 'test-webhook-salt'


def _table(data=None):
    t = MagicMock()
    t.select.return_value.eq.return_value.limit.return_value.execute.return_value = \
        MagicMock(data=data if data is not None else [])
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
    select(*).eq(order_type).eq(key).order(created_at).limit(1).execute()."""
    t = MagicMock()
    (t.select.return_value.eq.return_value.eq.return_value
       .order.return_value.limit.return_value.execute.return_value) = \
        MagicMock(data=rows)
    return t


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
    """Server-side price snapshot and HitPay payload construction."""

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

    def test_create_rejects_amount_mismatch(self, api_client):
        """Client amount deviating from race_categories.price is rejected."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(registrations=reg, race_categories=cat)):
            response = api_client.post(
                '/payments/create', data=self._payload(amount='60.00'),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 422
        assert 'mismatch' in response.json()['error'].lower()

    def test_create_snapshots_server_price(self, api_client):
        """The payments row stores the server-side price, not a client value."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        payments = _table()
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(registrations=reg, race_categories=cat,
                                         payments=payments)), \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-9', 'url': 'https://checkout.hit-pay.com/x',
                                 'status': 'pending'}):
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 201
        body = response.json()
        assert body['provider_request_id'] == 'pr-9'
        assert body['checkout_url'] == 'https://checkout.hit-pay.com/x'

        row = payments.insert.call_args.args[0]
        assert row['amount'] == '50.00'
        assert row['provider_request_id'] == 'pr-9'
        assert row['status'] == 'pending'
        assert row['order_type'] == 'registration'

    def test_create_sends_expected_hitpay_fields(self, api_client):
        """Form payload: send_sms=false, allow_repeated_payments=false,
        reference_number=registration_id, no deprecated webhook field."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(registrations=reg, race_categories=cat,
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
        assert any(k.startswith('payment_methods') for k in payload)

    def test_create_subscription_uses_client_amount(self, api_client):
        """organizer_subscription has no server-side price source."""
        payments = _table()
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
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
        row = payments.insert.call_args.args[0]
        assert row['amount'] == '299.00'
        assert row['order_type'] == 'organizer_subscription'

    def test_create_idempotent_returns_existing_pending_pr(self, api_client):
        """M1: a pending payment for the same order must be reused, not
        duplicated against the (rate-limited) HitPay create endpoint."""
        payments = _pending_payment_table([{
            'provider_request_id': 'pr-old',
            'status': 'pending',
            'qr_code_data': {'qr_code': 'qr-1'},
        }])
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(payments=payments)), \
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
        payments.insert.assert_not_called()

    def test_create_does_not_reuse_completed_payment(self, api_client):
        """M1 only dedupes *pending* payments; a completed payment gets a new PR."""
        reg = _table([{'category_id': 'cat-1'}])
        cat = _table([{'price': '50.00'}])
        payments = _pending_payment_table([{
            'provider_request_id': 'pr-old',
            'status': 'completed',
            'qr_code_data': {'qr_code': 'qr-1'},
        }])
        with patch('apps.payments.services.get_sb',
                   return_value=_mock_sb(registrations=reg,
                                         race_categories=cat, payments=payments)), \
             patch('apps.payments.services.get_payment_request') as mock_get, \
             patch('apps.payments.services.create_payment_request',
                   return_value={'id': 'pr-new', 'url': 'https://x', 'status': 'pending'}):
            response = api_client.post(
                '/payments/create', data=self._payload(),
                content_type='application/json', **_auth(),
            )
        assert response.status_code == 201
        mock_get.assert_not_called()


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
class TestRefund:
    def _completed_payment(self, rail):
        return _table([{
            'provider_request_id': 'pr-9',
            'provider_payment_id': 'pay-1',
            'payment_rail': rail,
            'status': 'completed',
            'amount': '10.00',
            'registration_id': 'reg-1',
        }])

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
        """M3: client amount must be numeric, > 0, ≤ stored amount."""
        payments = self._completed_payment('fpx')
        for bad_amount in ('-1', '0', 'abc', '99.00'):
            with patch('apps.payments.services.get_sb',
                       return_value=_mock_sb(payments=_table(
                           self._completed_payment('fpx')))), \
                 patch('apps.payments.services.create_refund') as mock_refund:
                response = api_client.post(
                    '/payments/refund',
                    data={'provider_request_id': 'pr-9', 'amount': bad_amount},
                    content_type='application/json', **_auth(),
                )
            assert response.status_code == 422, bad_amount
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
            status, body = services.refund_payment('pr-9', None)
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
