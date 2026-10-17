"""
HitPay payments flow tests: create / status / refund / reconcile,
plus internal-key middleware behaviour for the /payments/* surface.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import Client

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
             patch('apps.payments.hitpay.get_payment_request',
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
             patch('apps.payments.hitpay.get_payment_request',
                   return_value={'id': 'pr-9', 'status': 'expired'}):
            call_command('reconcile_payments')
        sb.rpc.assert_not_called()
        assert payments.update.call_args.args[0]['status'] == 'expired'

    def test_reconcile_poll_error_does_not_crash(self):
        payments = self._stale_pending()
        sb = _mock_sb(payments=payments)
        with patch('apps.payments.services.get_sb', return_value=sb), \
             patch('apps.payments.hitpay.get_payment_request',
                   side_effect=Exception('hitpay down')):
            call_command('reconcile_payments')
        sb.rpc.assert_not_called()
