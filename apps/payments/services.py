"""
Payments service — Supabase persistence + HitPay orchestration.

Django is stateless: every read/write goes through the Supabase client
with the service-role key (bypasses RLS, like the other apps/* services).
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings
from supabase import create_client

from .hitpay import create_payment_request, create_refund

logger = logging.getLogger(__name__)

# Rails HitPay cannot refund programmatically — disbursement happens
# manually outside the API.
NONREFUNDABLE_RAILS = {'duitnow', 'atome', 'wechat_pay', 'atome_qr'}

# Rails that can return an embedded QR payload when generate_qr is honoured.
QR_CAPABLE_RAILS = {'duitnow'}

RECONCILE_STALE_AFTER = timedelta(minutes=15)


def get_sb():
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY,
    )


# ── SIGNATURE VERIFICATION ───────────────────────────────────

def verify_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 hex of the raw body keyed by the webhook-endpoint salt.

    Fails closed: a signature produced with the legacy API-key salt (or
    any in-body `hmac` scheme) does not verify here.
    """
    if not signature or not settings.HITPAY_WEBHOOK_SALT:
        return False
    expected = hmac.new(
        settings.HITPAY_WEBHOOK_SALT.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _is_duplicate(exc: Exception) -> bool:
    text = str(exc).lower()
    return 'duplicate key' in text or '23505' in text


# ── WEBHOOK ──────────────────────────────────────────────────

def handle_webhook(raw_body: bytes, signature: str, event_type: str) -> tuple[int, dict]:
    """Process a HitPay webhook. Returns (http_status, body).

    Verified payloads always answer 200; dedupe replays via the
    webhook_events.payload_hash unique constraint.
    """
    if not verify_signature(raw_body, signature):
        return 403, {'error': 'Invalid signature'}

    try:
        data = json.loads(raw_body)
    except ValueError:
        return 400, {'error': 'Invalid JSON'}

    payload_hash = hashlib.sha256(raw_body).hexdigest()
    sb = get_sb()

    try:
        sb.table('webhook_events').insert({
            'provider': 'hitpay',
            'event_type': event_type,
            'object_id': data.get('id'),
            'payload': data,
            'payload_hash': payload_hash,
        }).execute()
    except Exception as exc:
        if _is_duplicate(exc):
            return 200, {'status': 'duplicate'}
        raise

    if event_type == 'payment_request.completed' and data.get('status') == 'completed':
        payments = data.get('payments') or []
        succeeded = next((p for p in payments if p.get('status') == 'succeeded'), None)
        sb.rpc('confirm_payment', {
            'p_provider_request_id': data.get('id'),
            'p_provider_payment_id': succeeded.get('id') if succeeded else None,
            'p_rail': succeeded.get('payment_type') if succeeded else None,
        }).execute()
    elif event_type == 'payment_request.failed':
        reason = None
        for p in data.get('payments') or []:
            if p.get('status_reason_code'):
                reason = p['status_reason_code']
                break
        sb.table('payments').update({
            'status': 'failed',
            'last_webhook_payload': data,
        }).eq('provider_request_id', data.get('id')).execute()
        logger.warning('HitPay payment request %s failed (%s)', data.get('id'), reason)

    return 200, {'status': 'ok'}


# ── CREATE ───────────────────────────────────────────────────

def _server_amount(sb, order_type: str, registration_id: str | None,
                   merch_order_id: str | None) -> float | None:
    """Read the authoritative amount server-side; None = trust client amount."""
    if order_type == 'registration' and registration_id:
        reg = (sb.table('registrations').select('category_id')
               .eq('id', registration_id).limit(1).execute())
        rows = reg.data or []
        if not rows:
            return None
        cat = (sb.table('race_categories').select('price')
               .eq('id', rows[0]['category_id']).limit(1).execute())
        cat_rows = cat.data or []
        if not cat_rows or cat_rows[0]['price'] is None:
            return None
        return float(cat_rows[0]['price'])
    if order_type == 'merch' and merch_order_id:
        items = (sb.table('merch_order_items').select('quantity,unit_price')
                 .eq('order_id', merch_order_id).execute())
        return sum(float(i['unit_price']) * int(i['quantity'])
                   for i in (items.data or []))
    return None


def create_payment(data: dict) -> tuple[int, dict]:
    """Create a HitPay payment request and snapshot the payment row."""
    order_type = data.get('order_type')
    registration_id = data.get('registration_id')
    merch_order_id = data.get('merch_order_id')
    organizer_id = data.get('organizer_id')
    runner_id = data.get('runner_id')
    try:
        client_amount = float(data.get('amount'))
    except (TypeError, ValueError):
        return 400, {'error': 'amount is required'}

    sb = get_sb()
    server_amount = _server_amount(sb, order_type, registration_id, merch_order_id)
    if server_amount is not None and abs(server_amount - client_amount) > 0.01:
        return 422, {'error': 'Amount mismatch with server-side price'}

    amount = server_amount if server_amount is not None else client_amount
    currency = data.get('currency', 'MYR')
    payment_methods = data.get('payment_methods') or [
        'fpx', 'duitnow', 'grabpay_direct', 'shopee_pay', 'touch_n_go',
    ]

    payload = {
        'amount': f'{amount:.2f}',
        'currency': currency,
        'email': data.get('email'),
        'name': data.get('name'),
        'purpose': data.get('purpose', 'Payment'),
        'redirect_url': data.get('redirect_url'),
        'allow_repeated_payments': 'false',
        'send_sms': 'false',
        'send_email': 'false',
    }
    for i, method in enumerate(payment_methods):
        payload[f'payment_methods[{i}]'] = method

    reference_number = registration_id or merch_order_id
    if reference_number:
        payload['reference_number'] = str(reference_number)
    if data.get('phone'):
        payload['phone'] = data['phone']
    # generate_qr only when a single QR-capable method is requested.
    if len(payment_methods) == 1 and payment_methods[0] in QR_CAPABLE_RAILS:
        payload['generate_qr'] = 'true'

    try:
        pr = create_payment_request(payload)
    except Exception as exc:
        from .hitpay import HitPayError
        if isinstance(exc, HitPayError):
            return 422, {'error': exc.message, 'errors': exc.errors}
        raise

    qr_code = None
    qr_data = pr.get('qr_code_data')
    if isinstance(qr_data, dict):
        qr_code = qr_data.get('qr_code')

    insert_row = {
        'order_type': order_type,
        'registration_id': registration_id,
        'merch_order_id': merch_order_id,
        'organizer_id': organizer_id,
        'runner_id': runner_id,
        'provider': 'hitpay',
        'provider_request_id': pr.get('id'),
        'amount': f'{amount:.2f}',
        'currency': currency,
        'status': pr.get('status', 'pending'),
        'qr_code_data': {'qr_code': qr_code} if qr_code else None,
    }
    sb.table('payments').insert(insert_row).execute()

    direct_link = pr.get('direct_link') or {}
    return 201, {
        'provider_request_id': pr.get('id'),
        'checkout_url': pr.get('url'),
        'qr_code': qr_code,
        'direct_link_url': direct_link.get('direct_link_url'),
        'status': pr.get('status', 'pending'),
    }


# ── STATUS ───────────────────────────────────────────────────

def get_payment_status(reference_number: str) -> tuple[int, dict]:
    """Latest payment row for a reference number (registration_id)."""
    sb = get_sb()
    result = (sb.table('payments')
              .select('status,payment_rail,provider_request_id')
              .eq('registration_id', reference_number)
              .order('created_at', desc=True)
              .limit(1)
              .execute())
    rows = result.data or []
    if not rows:
        return 404, {'error': 'Payment not found'}
    row = rows[0]
    return 200, {
        'status': row['status'],
        'payment_rail': row.get('payment_rail'),
        'provider_request_id': row.get('provider_request_id'),
    }


# ── REFUND ───────────────────────────────────────────────────

def refund_payment(provider_request_id: str, amount: str | None) -> tuple[int, dict]:
    """Refund a completed payment, refusing manual-disbursement rails."""
    sb = get_sb()
    result = (sb.table('payments').select('*')
              .eq('provider_request_id', provider_request_id)
              .limit(1).execute())
    rows = result.data or []
    if not rows:
        return 404, {'error': 'Payment not found'}
    payment = rows[0]

    if payment.get('payment_rail') in NONREFUNDABLE_RAILS:
        sb.table('payments').update({
            'refund_blocked_reason': 'manual_disbursement',
        }).eq('provider_request_id', provider_request_id).execute()
        return 422, {
            'error': 'Rail requires manual disbursement; refund not forwarded',
            'refund_blocked_reason': 'manual_disbursement',
        }

    if payment.get('status') not in ('completed', 'partially_refunded'):
        return 422, {'error': f"Payment is {payment.get('status')}, not refundable"}

    refund_amount = amount or str(payment['amount'])
    refund = create_refund(payment['provider_payment_id'], refund_amount)

    new_status = 'partially_refunded' if float(refund_amount) < float(payment['amount']) \
        else 'refunded'
    sb.table('payments').update({
        'status': new_status,
    }).eq('provider_request_id', provider_request_id).execute()

    sb.table('audit_log').insert({
        'action': 'payment_refunded',
        'target_id': payment.get('registration_id') or payment.get('organizer_id'),
        'metadata': {
            'provider_request_id': provider_request_id,
            'amount': refund_amount,
            'payment_rail': payment.get('payment_rail'),
        },
    }).execute()

    return 200, {'status': new_status, 'refund_id': refund.get('id')}


# ── RECONCILE ────────────────────────────────────────────────

def reconcile_stale_payments() -> dict:
    """Poll HitPay for pending payments older than RECONCILE_STALE_AFTER."""
    from .hitpay import get_payment_request

    sb = get_sb()
    cutoff = (datetime.now(timezone.utc) - RECONCILE_STALE_AFTER).isoformat()
    stale = (sb.table('payments').select('*')
             .eq('status', 'pending')
             .lt('created_at', cutoff)
             .execute())
    rows = stale.data or []

    summary = {'polled': 0, 'completed': 0, 'recorded': 0}
    for payment in rows:
        try:
            pr = get_payment_request(payment['provider_request_id'])
        except Exception as exc:
            logger.warning('Reconcile poll failed for %s: %s',
                           payment['provider_request_id'], exc)
            continue
        summary['polled'] += 1
        status = pr.get('status')

        if status == 'completed':
            payments = pr.get('payments') or []
            succeeded = next(
                (p for p in payments if p.get('status') == 'succeeded'), None)
            sb.rpc('confirm_payment', {
                'p_provider_request_id': payment['provider_request_id'],
                'p_provider_payment_id': succeeded.get('id') if succeeded else None,
                'p_rail': succeeded.get('payment_type') if succeeded else None,
            }).execute()
            summary['completed'] += 1
        elif status in ('failed', 'expired', 'canceled'):
            sb.table('payments').update({'status': status}) \
                .eq('provider_request_id', payment['provider_request_id']).execute()
            summary['recorded'] += 1

        sb.table('audit_log').insert({
            'action': 'payment_reconciled',
            'target_id': payment.get('registration_id') or payment.get('organizer_id'),
            'metadata': {
                'provider_request_id': payment['provider_request_id'],
                'hitpay_status': status,
            },
        }).execute()

    return summary
