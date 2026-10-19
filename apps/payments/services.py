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
from decimal import Decimal, InvalidOperation

from django.conf import settings
from supabase import create_client

from .hitpay import (HitPayError, create_payment_request, create_refund,
                     get_payment_request)

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
    # L3: hex digests are case-insensitive; trim padding before comparing.
    return hmac.compare_digest(expected, signature.strip().lower())


def _is_duplicate(exc: Exception) -> bool:
    text = str(exc).lower()
    return 'duplicate key' in text or '23505' in text


# ── WEBHOOK ──────────────────────────────────────────────────

def _audit_amount_mismatch(sb, data: dict, succeeded: dict | None) -> None:
    """M6: detect under/overcharge. Confirmation always proceeds (the
    customer paid what HitPay charged) — the mismatch is audit-logged."""
    if not succeeded or not succeeded.get('amount'):
        return
    result = (sb.table('payments').select('amount')
              .eq('provider_request_id', data.get('id')).limit(1).execute())
    rows = result.data if isinstance(result.data, list) else []
    if not rows or rows[0].get('amount') is None:
        return
    stored_amount = Decimal(str(rows[0]['amount']))
    charge_amount = Decimal(str(succeeded['amount']))
    if abs(stored_amount - charge_amount) > Decimal('0.01'):
        sb.table('audit_log').insert({
            'action': 'payment_amount_mismatch',
            'target_id': data.get('reference_number'),
            'metadata': {
                'provider_request_id': data.get('id'),
                'pr_amount': str(stored_amount),
                'charge_amount': str(charge_amount),
            },
        }).execute()
        logger.warning(
            'payment_amount_mismatch: PR %s expected %s, charge %s',
            data.get('id'), stored_amount, charge_amount,
        )


def _handle_verified_event(sb, event_type: str, data: dict) -> None:
    """Act on a verified webhook payload (insert of the dedupe row is done)."""
    if event_type == 'payment_request.completed' and data.get('status') == 'completed':
        payments = data.get('payments') or []
        succeeded = next((p for p in payments if p.get('status') == 'succeeded'), None)
        try:
            _audit_amount_mismatch(sb, data, succeeded)
        except Exception:
            # Advisory check — a failure here must never block confirmation.
            logger.exception('payment_amount_mismatch check failed for %s',
                             data.get('id'))
        sb.rpc('confirm_payment', {
            'p_provider_request_id': data.get('id'),
            'p_provider_payment_id': succeeded.get('id') if succeeded else None,
            'p_rail': succeeded.get('payment_type') if succeeded else None,
        }).execute()
    elif event_type == 'payment_request.failed':
        # L4: without an object id the failure cannot be scoped to a row —
        # a .eq(None) filter would sweep unmatched rows. Log and skip.
        if not data.get('id'):
            logger.warning('payment_request.failed without object id; skipping event')
            return
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

    # HitPay sends the event name as either the dotted form
    # ('payment_request.completed') or the bare kind ('completed') —
    # normalize so both hit the same branch (live-sandbox observed 2026-10-18).
    normalized_event = (event_type or '').strip().lower()
    if normalized_event and '.' not in normalized_event:
        if data.get('object') == 'payment_request':
            normalized_event = f'payment_request.{normalized_event}'

    try:
        _handle_verified_event(sb, normalized_event, data)
    except Exception:
        # H1: the dedupe row already exists, so a 500 here would swallow the
        # verified event forever (the redelivery would hit the payload_hash
        # unique constraint). Release the row (best-effort) so HitPay's
        # redelivery reprocesses, and answer 200 either way. Log loudly —
        # event type and object id only, never payload or secrets.
        logger.exception(
            'Webhook handler failed event_type=%s object_id=%s; releasing '
            'webhook_events row for redelivery', event_type, data.get('id'))
        try:
            sb.table('webhook_events').delete() \
                .eq('payload_hash', payload_hash).execute()
        except Exception:
            logger.exception('Could not release webhook_events row '
                             '(object_id=%s)', data.get('id'))
        return 200, {'status': 'handler_error'}

    return 200, {'status': 'ok'}


# ── CREATE ───────────────────────────────────────────────────

def _server_amount(sb, order_type: str, registration_id: str | None,
                   merch_order_id: str | None) -> float | None:
    """Read the authoritative amount server-side; None = no server price
    source (organizer_subscription only)."""
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


def _reuse_pending_payment(existing: dict) -> tuple[int, dict]:
    """Return the existing pending PR (200) without hitting the create
    endpoint; re-fetch the PR from HitPay for the freshest checkout URL."""
    pr: dict = {}
    try:
        pr = get_payment_request(existing['provider_request_id'])
    except Exception:
        logger.warning('Could not re-fetch HitPay PR %s; reusing stored payment row',
                       existing['provider_request_id'], exc_info=True)
    qr = (existing.get('qr_code_data') or {}).get('qr_code')
    return 200, {
        'provider_request_id': existing['provider_request_id'],
        'checkout_url': pr.get('url'),
        'qr_code': qr,
        'direct_link_url': (pr.get('direct_link') or {}).get('direct_link_url'),
        'status': pr.get('status', existing.get('status')),
    }


# Sentinel provider_request_id that migration 062's claim_payment_slot
# writes on a reservation row: the caller swaps it for the real HitPay PR
# id once the upstream create succeeds (fixed formatter pin, see docs).
RESERVATION_PREFIX = 'pending:'


def create_payment(data: dict) -> tuple[int, dict]:
    """Create a HitPay payment request and snapshot the payment row.

    Race-safe via migration 062's atomic claim_payment_slot: the claim runs
    FIRST, before any HitPay API call. An existing non-reservation row means
    another caller already created the PR (M1 reuse, no HitPay call); a
    reservation row carries the `pending:<uuid>` sentinel provider id and is
    swapped for the real one after the upstream create (or deleted on
    failure, atomically releasing the slot).
    """
    order_type = data.get('order_type')
    registration_id = data.get('registration_id')
    merch_order_id = data.get('merch_order_id')
    organizer_id = data.get('organizer_id')
    runner_id = data.get('runner_id')

    sb = get_sb()

    # Amount authority is always server-side: registration/merch derive it
    # from the DB price sources and the caller's number is ignored entirely;
    # subscriptions have no server price source, so the caller's amount is
    # used but must be positive.
    amount = _server_amount(sb, order_type, registration_id, merch_order_id)
    if amount is None or amount <= 0:
        if order_type == 'organizer_subscription':
            try:
                amount = float(data.get('amount'))
            except (TypeError, ValueError):
                amount = None
        if not amount or amount <= 0:
            return 422, {'error': 'Invalid amount'}

    currency = (data.get('currency') or 'MYR').strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        return 422, {'error': 'Invalid currency'}

    # Atomic double-charge guard (062): claim the payment slot before any
    # HitPay call. is_reservation=false → an existing pending row for this
    # order already exists; reuse it (no second HitPay PR — double charge).
    claim_result = sb.rpc('claim_payment_slot', {
        'p_order_type': order_type,
        'p_registration_id': registration_id,
        'p_merch_order_id': merch_order_id,
        'p_organizer_id': organizer_id,
        'p_runner_id': runner_id,
        'p_amount': f'{amount:.2f}',
        'p_currency': currency,
    }).execute()
    claim_rows = claim_result.data if isinstance(claim_result.data, list) else []
    claim_row = claim_rows[0] if claim_rows else None
    if not claim_row or not claim_row.get('provider_request_id'):
        logger.error('claim_payment_slot returned no slot (order_type=%s)',
                     order_type)
        return 409, {'error': 'Could not reserve a payment slot; try again'}

    provider_request_id = str(claim_row['provider_request_id'])

    if not claim_row.get('is_reservation'):
        # M1: a pending PR already exists for this order — reuse it.
        existing = (sb.table('payments').select('*')
                    .eq('provider_request_id', provider_request_id)
                    .limit(1).execute())
        rows = existing.data or []
        if rows:
            return _reuse_pending_payment(rows[0])
        return 200, {'provider_request_id': provider_request_id,
                     'checkout_url': None, 'status': 'pending'}

    # payment_methods: OMIT by default — HitPay then uses every rail toggled
    # ON in the merchant Dashboard (strict per-element validation rejects the
    # whole request if any one element isn't currently enabled). The key is
    # only sent when HITPAY_PAYMENT_METHODS or the caller provides non-empty
    # methods, comma-separated, trimmed, lowercased to HitPay enums.
    env_methods_raw = settings.HITPAY_PAYMENT_METHODS
    caller_methods_raw = data.get('payment_methods')
    if env_methods_raw.strip():
        methods_source = env_methods_raw
    elif isinstance(caller_methods_raw, (list, tuple)):
        methods_source = ','.join(str(m) for m in caller_methods_raw)
    else:
        methods_source = str(caller_methods_raw or '').strip()
    payment_methods = [m.strip().lower() for m in methods_source.split(',')
                       if m.strip()]

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

    # Reservation row: provider_request_id is the `pending:<uuid>` sentinel.
    # Call HitPay, then swap the sentinel for the real PR id. On failure the
    # sentinel row is deleted — the claim is released atomically and the
    # next attempt starts fresh.
    try:
        pr = create_payment_request(payload)
    except HitPayError as exc:
        try:
            sb.table('payments').delete() \
                .eq('provider_request_id', provider_request_id).execute()
        except Exception:
            logger.exception('Could not release reservation row %s',
                             provider_request_id)
        return 422, {'error': exc.message, 'errors': exc.errors}

    qr_code = None
    qr_data = pr.get('qr_code_data')
    if isinstance(qr_data, dict):
        qr_code = qr_data.get('qr_code')

    sb.table('payments').update({
        'provider_request_id': pr.get('id'),
        'provider': 'hitpay',
        'status': pr.get('status', 'pending'),
        'qr_code_data': {'qr_code': qr_code} if qr_code else None,
    }).eq('provider_request_id', provider_request_id).execute()

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

def _validated_refund_amount(amount: str | None, stored_amount: str,
                             refunded_amount) -> tuple[str | None, str | None]:
    """M3 + 062 hardening: a client-supplied amount must be numeric and
    ≤ the *remaining* refundable amount (stored amount − refunded_amount,
    0.01 tolerance for rounding). A missing amount means "refund the
    remaining balance".

    Returns (amount, None) on success or (None, reason) on failure — the
    reason is our own constant text (never exception internals), keeping
    exception details out of externally-visible bodies (CodeQL GAS 12).
    """
    try:
        stored = Decimal(str(stored_amount))
        already = Decimal(str(refunded_amount)) if refunded_amount is not None \
            else Decimal('0')
        refund_amount = (Decimal(str(amount)) if amount is not None
                         else stored - already)
        remaining = stored - already
        if refund_amount <= 0:
            return None, 'Refund amount must be greater than zero'
        if refund_amount > remaining + Decimal('0.01'):
            return None, 'Refund amount exceeds the remaining refundable amount'
    except InvalidOperation:
        return None, 'Refund amount must be a number'
    return f'{refund_amount:.2f}', None


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

    # M2: refunding needs the charge id, not the payment-request id.
    # W2 alignment: a completed row whose provider_payment_id hasn't landed
    # yet (webhook/061 confirm pending) gets NO refund-block marker — it
    # must stay claimable for the moment the charge reference arrives.
    if not payment.get('provider_payment_id'):
        return 409, {
            'error': 'no charge reference; manual disbursement or reconcile first',
            'refund_blocked_reason': 'no_charge_reference',
        }

    refund_amount, amount_error = _validated_refund_amount(
        amount, payment['amount'], payment.get('refunded_amount'))
    if amount_error or refund_amount is None:
        return 422, {'error': amount_error or 'Invalid refund amount'}

    # H2: atomic double-refund guard. `status` is CHECK-constrained (there is
    # no 'refunding' value), so claim the row via the free-text
    # refund_blocked_reason marker instead: the conditional update succeeds
    # for exactly one concurrent caller (WHERE status refundable AND marker
    # NULL). A 0-row update means another request already holds the claim or
    # finalised the refund → 409. Only the winner may call HitPay.
    claim = (sb.table('payments')
             .update({'refund_blocked_reason': 'refund_in_progress'})
             .eq('provider_request_id', provider_request_id)
             .in_('status', ('completed', 'partially_refunded'))
             .is_('refund_blocked_reason', 'null')
             .execute())
    if not (claim.data or []):
        return 409, {
            'error': 'already refunded or refund already in progress',
            'refund_blocked_reason': 'refund_in_progress',
        }

    try:
        refund = create_refund(payment['provider_payment_id'], refund_amount)
    except HitPayError as exc:
        if exc.status is not None and exc.status < 500:
            # HitPay rejected the refund outright — no outcome ambiguity,
            # release the claim so a corrected retry can claim again.
            logger.warning('Refund rejected by HitPay for %s', provider_request_id)
            sb.table('payments').update({'refund_blocked_reason': None}) \
                .eq('provider_request_id', provider_request_id).execute()
            return 502, {'error': 'Refund failed at HitPay'}
        # 5xx/timeouts/transport errors: the refund outcome is UNKNOWN.
        # Keep the claim but under a distinct marker (never surfaced as
        # auto-refundable) so reconciliation/manual review resolves truth.
        logger.exception(
            'Refund at HitPay failed with unknown outcome for %s — claim '
            'kept as refund_unknown for reconciliation',
            provider_request_id)
        sb.table('payments').update({
            'refund_blocked_reason': 'refund_unknown',
        }).eq('provider_request_id', provider_request_id) \
          .eq('refund_blocked_reason', 'refund_in_progress').execute()
        return 502, {'error': 'Refund failed at HitPay'}
    except Exception:
        # Transport failure (timeout, DNS, …) — outcome unknown; same
        # reconciliation path. The detail is logged server-side without
        # secrets; the caller gets a stable generic body.
        logger.exception(
            'Refund request to HitPay failed mid-flight for %s — claim '
            'kept as refund_unknown', provider_request_id)
        sb.table('payments').update({
            'refund_blocked_reason': 'refund_unknown',
        }).eq('provider_request_id', provider_request_id) \
          .eq('refund_blocked_reason', 'refund_in_progress').execute()
        return 502, {'error': 'Refund failed at HitPay'}

    # 062: refunded_amount is cumulative; status derives from the FULL
    # remaining balance, not just this refund vs the total.
    stored = Decimal(str(payment['amount']))
    already_refunded = Decimal(str(payment.get('refunded_amount') or 0))
    total_refunded = already_refunded + Decimal(refund_amount)
    new_status = ('refunded' if total_refunded >= stored - Decimal('0.01')
                  else 'partially_refunded')
    sb.table('payments').update({
        'status': new_status,
        'refunded_amount': f'{total_refunded:.2f}',
        'refund_blocked_reason': None,
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

def _reconcile_claim_hash(provider_request_id: str) -> str:
    """Deterministic dedupe key for a reconcile-poll claim of one payment."""
    return hashlib.sha256(f'reconcile:{provider_request_id}'.encode()).hexdigest()


# L2: a reconcile claim older than this is considered orphaned (hard cron
# kill) and is reclaimed by the next run.
RECONCILE_CLAIM_TTL = timedelta(minutes=10)


def _reconcile_claim_orphaned(sb, claim_hash: str) -> bool:
    """True when an existing reconcile.poll claim row is older than the TTL.

    Scoped by the exact claim payload_hash — real webhook event rows are
    never inspected or touched by this.
    """
    row = (sb.table('webhook_events').select('received_at')
           .eq('payload_hash', claim_hash)
           .maybe_single().execute())
    # Null-guard: no row (or non-dict payload) → can't verify age.
    data = row.data if isinstance(row.data, dict) else {}
    received = data.get('received_at')
    if not isinstance(received, str):
        return False  # can't verify age → keep the conservative skip
    try:
        ts = datetime.fromisoformat(received.replace('Z', '+00:00'))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - ts) > RECONCILE_CLAIM_TTL


def reconcile_stale_payments() -> dict:
    """Poll HitPay for pending payments older than RECONCILE_STALE_AFTER.

    L2: each stale row is claimed by inserting a (unique) webhook_events row
    keyed off the provider_request_id BEFORE polling — overlapping cron runs
    hit the payload_hash unique constraint and skip already-claimed rows, so
    there is no double-poll or double-audit. Claims older than
    RECONCILE_CLAIM_TTL are treated as orphaned (hard cron kill) and
    reclaimed; successful runs release their claim after processing.
    """
    sb = get_sb()
    cutoff = (datetime.now(timezone.utc) - RECONCILE_STALE_AFTER).isoformat()
    stale = (sb.table('payments').select('*')
             .eq('status', 'pending')
             .lt('created_at', cutoff)
             .execute())
    rows = stale.data or []

    summary = {'polled': 0, 'completed': 0, 'recorded': 0}
    for payment in rows:
        provider_request_id = payment['provider_request_id']
        claim_hash = _reconcile_claim_hash(provider_request_id)
        try:
            sb.table('webhook_events').insert({
                'provider': 'hitpay',
                'event_type': 'reconcile.poll',
                'object_id': provider_request_id,
                'payload': {
                    'type': 'reconcile_claim',
                    'ran_at': datetime.now(timezone.utc).isoformat(),
                },
                'payload_hash': claim_hash,
            }).execute()
        except Exception as exc:
            if _is_duplicate(exc):
                # L2: short claim rows taken by a killed cron run must not pin
                # a payment forever — reclaim after the TTL.
                if _reconcile_claim_orphaned(sb, claim_hash):
                    try:
                        sb.table('webhook_events').delete() \
                            .eq('payload_hash', claim_hash).execute()
                    except Exception:
                        logger.exception(
                            'Could not reclaim orphaned reconcile claim for %s',
                            provider_request_id)
                        continue
                else:
                    logger.info('Reconcile: row %s already claimed by another '
                                'run; skipping', provider_request_id)
                    continue
            else:
                raise

        try:
            try:
                pr = get_payment_request(provider_request_id)
            except Exception as exc:
                logger.warning('Reconcile poll failed for %s: %s',
                               provider_request_id, exc)
                raise
            summary['polled'] += 1
            status = pr.get('status')

            if status == 'completed':
                payments = pr.get('payments') or []
                succeeded = next(
                    (p for p in payments if p.get('status') == 'succeeded'), None)
                sb.rpc('confirm_payment', {
                    'p_provider_request_id': provider_request_id,
                    'p_provider_payment_id': succeeded.get('id') if succeeded else None,
                    'p_rail': succeeded.get('payment_type') if succeeded else None,
                }).execute()
                # W2: the charge reference is now known (confirm_payment wrote
                # provider_payment_id), so a stuck 'no_charge_reference' marker
                # must be released — otherwise refunds 409 forever. Guarded:
                # H2 'refund_in_progress' / 'manual_disbursement' markers stay.
                if succeeded and succeeded.get('id'):
                    sb.table('payments').update({
                        'refund_blocked_reason': None,
                    }).eq('provider_request_id', provider_request_id) \
                      .eq('refund_blocked_reason', 'no_charge_reference') \
                      .execute()
                summary['completed'] += 1
            elif status in ('failed', 'expired', 'canceled'):
                sb.table('payments').update({'status': status}) \
                    .eq('provider_request_id', provider_request_id).execute()
                summary['recorded'] += 1

            sb.table('audit_log').insert({
                'action': 'payment_reconciled',
                'target_id': payment.get('registration_id') or payment.get('organizer_id'),
                'metadata': {
                    'provider_request_id': provider_request_id,
                    'hitpay_status': status,
                },
            }).execute()

            # L2: success → release the claim so future stale runs can
            # re-poll this PR if it ever goes stale again.
            sb.table('webhook_events').delete() \
                .eq('payload_hash', claim_hash).execute()
        except Exception:
            # Release the claim so the next run retries this row.
            logger.exception('Reconcile processing failed for %s — releasing claim',
                             provider_request_id)
            try:
                sb.table('webhook_events').delete() \
                    .eq('payload_hash', claim_hash).execute()
            except Exception:
                logger.exception('Could not release reconcile claim for %s',
                                 provider_request_id)
            continue

    return summary
