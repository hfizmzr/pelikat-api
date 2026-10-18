"""
HitPay webhook signature security tests (NFR: only signature-verified
webhook payloads may transition a payment to completed).

HitPay signs the RAW request body with HMAC-SHA256 using the per-endpoint
webhook salt (Dashboard -> Developers). The legacy scheme signs an `hmac`
field inside redirect payloads with the API-key salt — verifying against
that salt (or trusting an in-body `hmac` field) must FAIL CLOSED.
"""
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client

WEBHOOK_PATH = '/payments/webhook/hitpay/'
WEBHOOK_SALT = 'test-webhook-salt'
LEGACY_SALT = 'legacy-api-key-salt'


def _sign(body: bytes, salt: str = WEBHOOK_SALT) -> str:
    return hmac.new(salt.encode(), body, hashlib.sha256).hexdigest()


def _hash(body: bytes) -> str:
    """Payload-hash the service stores in webhook_events for dedupe."""
    return hashlib.sha256(body).hexdigest()


def _headers(body: bytes, event: str = 'payment_request.completed',
             signature: str | None = None) -> dict:
    return {
        'HTTP_HITPAY_SIGNATURE': signature if signature is not None else _sign(body),
        'HTTP_HITPAY_EVENT_TYPE': event,
    }


def _pr_body(status: str = 'completed', payment_id: str = 'pay-1',
             rail: str = 'grabpay_direct') -> bytes:
    body = {
        'id': 'pr-123',
        'object': 'payment_request',
        'status': status,
        'reference_number': 'reg-123',
        'payments': [],
    }
    if payment_id:
        body['payments'] = [{
            'id': payment_id,
            'status': 'succeeded',
            'amount': '10.00',
            'payment_type': rail,
        }]
    return json.dumps(body).encode()


@pytest.mark.security
class TestHitpayWebhookSignature:
    """Verify Hitpay-Signature (HMAC-SHA256 of raw body with webhook salt)."""

    def test_webhook_missing_signature_rejected(self, api_client):
        """Missing Hitpay-Signature header fails closed with 403."""
        response = api_client.post(
            WEBHOOK_PATH, data=_pr_body(), content_type='application/json'
        )
        assert response.status_code == 403

    def test_webhook_invalid_signature_rejected(self, api_client):
        """A wrong signature fails closed with 403."""
        response = api_client.post(
            WEBHOOK_PATH, data=_pr_body(), content_type='application/json',
            HTTP_HITPAY_SIGNATURE='deadbeef' * 8,
        )
        assert response.status_code == 403

    def test_webhook_rejects_legacy_salt(self, api_client):
        """A signature made with the legacy API-key salt must NOT verify."""
        body = _pr_body()
        body_with_legacy_hmac_field = json.dumps({
            **json.loads(body),
            'hmac': _sign(body, salt=LEGACY_SALT),
        }).encode()
        response = api_client.post(
            WEBHOOK_PATH, data=body_with_legacy_hmac_field,
            content_type='application/json',
            HTTP_HITPAY_SIGNATURE=_sign(body_with_legacy_hmac_field,
                                        salt=LEGACY_SALT),
        )
        assert response.status_code == 403

    def test_webhook_tampered_body_rejected(self, api_client):
        """A valid signature over the original body must not verify a tampered body."""
        original = _pr_body()
        tampered = json.dumps({
            **json.loads(original),
            'status': 'completed',
            'payments': [{'id': 'attacker-pay', 'status': 'succeeded',
                          'amount': '10.00', 'payment_type': 'fpx'}],
        }).encode()
        response = api_client.post(
            WEBHOOK_PATH, data=tampered, content_type='application/json',
            HTTP_HITPAY_SIGNATURE=_sign(original),
        )
        assert response.status_code == 403

    def test_webhook_uses_compare_digest(self):
        """Verification must use constant-time comparison (hmac.compare_digest)."""
        import inspect
        from apps.payments import services

        source = inspect.getsource(services.verify_signature)
        assert 'compare_digest' in source
        assert 'hmac' in source or 'hmac.new' in source

    def test_webhook_valid_signature_confirms_payment(self, api_client):
        """A correctly signed payment_request.completed triggers confirm_payment."""
        mock_sb = MagicMock()
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=_pr_body(), content_type='application/json',
                HTTP_HITPAY_SIGNATURE=_sign(_pr_body()),
                HTTP_HITPAY_EVENT_TYPE='payment_request.completed',
            )
        assert response.status_code == 200
        mock_sb.rpc.assert_called_once_with(
            'confirm_payment',
            {
                'p_provider_request_id': 'pr-123',
                'p_provider_payment_id': 'pay-1',
                'p_rail': 'grabpay_direct',
            },
        )

    def test_webhook_failed_event_records_failure(self, api_client):
        """payment_request.failed marks the payment failed and never confirms."""
        mock_sb = MagicMock()
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=_pr_body(status='failed', payment_id=None),
                content_type='application/json',
                HTTP_HITPAY_SIGNATURE=_sign(_pr_body(status='failed',
                                                     payment_id=None)),
                HTTP_HITPAY_EVENT_TYPE='payment_request.failed',
            )
        assert response.status_code == 200
        mock_sb.rpc.assert_not_called()
        mock_sb.table.assert_any_call('payments')
        mock_sb.table.return_value.update.assert_called_once()
        update_kwargs = mock_sb.table.return_value.update.call_args.args[0]
        assert update_kwargs['status'] == 'failed'

    def test_webhook_duplicate_is_noop(self, api_client):
        """An identical redelivery must be a 200 no-op (payload_hash dedupe)."""
        mock_sb = MagicMock()
        body = _pr_body()
        headers = {
            'HTTP_HITPAY_SIGNATURE': _sign(body),
            'HTTP_HITPAY_EVENT_TYPE': 'payment_request.completed',
        }
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            first = api_client.post(WEBHOOK_PATH, data=body,
                                    content_type='application/json', **headers)
            assert first.status_code == 200

            # Second delivery: the webhook_events insert hits the unique
            # payload_hash constraint.
            mock_sb.table.return_value.insert.return_value.execute.side_effect = (
                Exception('duplicate key value violates unique constraint '
                          '"webhook_events_payload_hash_key"')
            )
            mock_sb.rpc.reset_mock()
            second = api_client.post(WEBHOOK_PATH, data=body,
                                     content_type='application/json', **headers)
        assert second.status_code == 200
        mock_sb.rpc.assert_not_called()


@pytest.mark.security
class TestWebhookEventTypeHeaderContract:
    """Live sandbox/webhooks send the short event name ('completed',
    'failed') in Hitpay-Event-Type, not the dotted
    'payment_request.completed' form — both must trigger the same action."""

    def _post(self, api_client, mock_sb, body, event):
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            return api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                HTTP_HITPAY_SIGNATURE=_sign(body),
                HTTP_HITPAY_EVENT_TYPE=event,
            )

    def test_short_completed_event_confirms_payment(self, api_client):
        """Reproduced live: header 'completed' must call confirm_payment."""
        mock_sb = MagicMock()
        body = _pr_body()
        response = self._post(api_client, mock_sb, body, 'completed')
        assert response.status_code == 200
        mock_sb.rpc.assert_called_once_with(
            'confirm_payment',
            {
                'p_provider_request_id': 'pr-123',
                'p_provider_payment_id': 'pay-1',
                'p_rail': 'grabpay_direct',
            },
        )

    def test_short_failed_event_records_failure(self, api_client):
        mock_sb = MagicMock()
        body = _pr_body(status='failed', payment_id=None)
        response = self._post(api_client, mock_sb, body, 'failed')
        assert response.status_code == 200
        mock_sb.rpc.assert_not_called()
        mock_sb.table.assert_any_call('payments')
        update_kwargs = mock_sb.table.return_value.update.call_args.args[0]
        assert update_kwargs['status'] == 'failed'

    def test_dotted_event_type_still_confirms(self, api_client):
        """The legacy dotted form must keep working after normalization."""
        mock_sb = MagicMock()
        response = self._post(api_client, mock_sb, _pr_body(),
                              'payment_request.completed')
        assert response.status_code == 200
        mock_sb.rpc.assert_called_once()


@pytest.mark.security
class TestWebhookSignatureNormalization:
    """L3: hex digests are case-insensitive; whitespace must not matter."""

    def test_uppercase_hex_signature_accepted(self, api_client):
        body = _pr_body()
        mock_sb = MagicMock()
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                **_headers(body, signature=_sign(body).upper()),
            )
        assert response.status_code == 200

    def test_whitespace_padded_signature_accepted(self, api_client):
        body = _pr_body()
        mock_sb = MagicMock()
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                **_headers(body, signature='  ' + _sign(body).upper() + ' '),
            )
        assert response.status_code == 200


@pytest.mark.security
class TestWebhookAmountMismatchAudit:
    """M6: under/overcharge detection — confirm proceeds, mismatch audited."""

    def _mismatch_mock(self, stored_amount: str) -> MagicMock:
        mock_sb = MagicMock()
        mock_sb.table.return_value.select.return_value.eq.return_value \
            .limit.return_value.execute.return_value = \
            MagicMock(data=[{'amount': stored_amount}])
        return mock_sb

    def test_amount_mismatch_audited_but_still_confirmed(self, api_client):
        """Charge 10.00 vs PR 8.00 → audit row + normal confirmation."""
        mock_sb = self._mismatch_mock('8.00')
        body = _pr_body(payment_id='pay-1', rail='fpx')
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                **_headers(body),
            )
        assert response.status_code == 200
        mock_sb.rpc.assert_called_once_with(
            'confirm_payment',
            {'p_provider_request_id': 'pr-123',
             'p_provider_payment_id': 'pay-1', 'p_rail': 'fpx'},
        )
        mock_sb.table.return_value.insert.assert_any_call({
            'action': 'payment_amount_mismatch',
            'target_id': 'reg-123',
            'metadata': {
                'provider_request_id': 'pr-123',
                'pr_amount': '8.00',
                'charge_amount': '10.00',
            },
        })

    def test_matching_amount_no_mismatch_audit(self, api_client):
        mock_sb = self._mismatch_mock('10.00')
        body = _pr_body(payment_id='pay-1', rail='fpx')
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                **_headers(body),
            )
        assert response.status_code == 200
        mismatches = [
            c.args[0] for c in mock_sb.table.return_value.insert.call_args_list
            if c.args and c.args[0].get('action') == 'payment_amount_mismatch'
        ]
        assert mismatches == []


@pytest.mark.security
class TestWebhookHandlerFailureRelease:
    """H1: a crash after the dedupe insert must release the row so a
    HitPay redelivery reprocesses the event instead of being dedupe-blocked."""

    def test_rpc_failure_returns_200_and_releases_event_row(self, api_client):
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.side_effect = \
            Exception('confirm_payment blew up')
        body = _pr_body()
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                **_headers(body),
            )
        assert response.status_code == 200  # never 500 to HitPay
        mock_sb.table.return_value.delete.return_value.eq \
            .assert_called_once_with('payload_hash', _hash(body))

    def test_redelivery_after_release_reprocesses_event(self, api_client):
        """With the released row, a redelivery runs the handler again."""
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.side_effect = [
            Exception('confirm_payment blew up'), MagicMock(data=[]),
        ]
        body = _pr_body()
        headers = _headers(body)
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            first = api_client.post(WEBHOOK_PATH, data=body,
                                    content_type='application/json', **headers)
            second = api_client.post(WEBHOOK_PATH, data=body,
                                     content_type='application/json', **headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert mock_sb.rpc.call_count == 2

    def test_failed_event_without_id_skipped_silently(self, api_client):
        """L4: payment_request.failed with no object id — log + skip, no query."""
        body = json.dumps({
            'object': 'payment_request',
            'status': 'failed',
        }).encode()
        mock_sb = MagicMock()
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                **_headers(body, event='payment_request.failed'),
            )
        assert response.status_code == 200
        assert mock_sb.table.return_value.update.call_count == 0
        mock_sb.rpc.assert_not_called()

    def test_webhook_always_200_for_verified_payloads(self, api_client):
        """Verified payloads return 200 even for unrecognised event types."""
        mock_sb = MagicMock()
        body = json.dumps({'id': 'pr-xyz', 'status': 'pending'}).encode()
        with patch('apps.payments.services.get_sb', return_value=mock_sb):
            response = api_client.post(
                WEBHOOK_PATH, data=body, content_type='application/json',
                HTTP_HITPAY_SIGNATURE=_sign(body),
                HTTP_HITPAY_EVENT_TYPE='payment_request.some_new_event',
            )
        assert response.status_code == 200
        mock_sb.rpc.assert_not_called()
