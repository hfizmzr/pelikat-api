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
                     delete_payment_request, get_payment_request,
                     list_payment_requests)

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


def _revive_expired_payment(sb, pr_id: str | None) -> bool:
    """F2: a row set to 'expired' locally (amount-mismatch release: the
    stale checkout was superseded by a fresh one at the new price) can
    still be paid through its old checkout URL until the remote PR is
    cancelled. The customer DID pay a price they saw, so a late completed
    webhook must finalize, not silently drop. confirm_payment no-ops on
    non-pending rows, so re-arm the expired row to 'pending' (guarded to
    still-expired) and let confirm_payment complete it and write the
    audit. Returns True when a revive actually happened."""
    if not pr_id:
        return False
    existing = (sb.table('payments').select('id, status')
                .eq('provider_request_id', pr_id).limit(1).execute())
    rows = existing.data if isinstance(existing.data, list) else []
    if not rows or rows[0].get('status') != 'expired':
        return False
    (sb.table('payments').update({'status': 'pending'})
     .eq('provider_request_id', pr_id)
     .eq('status', 'expired').execute())
    logger.warning(
        'late completed webhook for locally-expired payment (PR %s) — '
        'customer paid the price they saw; re-armed row for '
        'confirm_payment', pr_id)
    return True


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
        try:
            _revive_expired_payment(sb, data.get('id'))
        except Exception:
            # Best-effort unblock — on failure confirm_payment simply
            # no-ops on the still-expired row (status quo).
            logger.exception('expired-payment revive failed for %s',
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
    # Race guard: if the local row is still pending but HitPay already
    # shows completed, do NOT surface completion before confirm_payment
    # has audited locally — the webhook/reconcile idempotent confirm
    # finalizes the row, and subsequent status GETs report the truth.
    status = pr.get('status', existing.get('status'))
    if existing.get('status') == 'pending' and status != 'pending':
        status = existing['status']
    return 200, {
        'provider_request_id': existing['provider_request_id'],
        'checkout_url': pr.get('url'),
        'qr_code': qr,
        'direct_link_url': (pr.get('direct_link') or {}).get('direct_link_url'),
        'status': status,
    }


# Sentinel provider_request_id that migration 062's claim_payment_slot
# writes on a reservation row: the caller swaps it for the real HitPay PR
# id once the upstream create succeeds (fixed formatter pin, see docs).
RESERVATION_PREFIX = 'pending:'

# An in_progress sentinel is recoverable (adopt-or-delete) only after this
# much time: younger sentinels are almost certainly a LIVE create still in
# flight (upstream timeout is 20s), and mutating them would race the
# winner's linking update. Older ones are crash residue (064 runbook).
SENTINEL_RECOVER_AFTER = timedelta(minutes=2)

# metadata key HitPay PRs carry after the recovery change: 'order_type:target'
# (one key → respects the list endpoint's "first metadata pair wins" filter).
SENTINEL_METADATA_KEY = 'metadata[pelikat_order]'


def _split_target(registration_id, merch_order_id, organizer_id):
    """The one target-scoped column/value pair for this order (the same
    keys claim_payment_slot serializes on)."""
    for column, value in (('registration_id', registration_id),
                          ('merch_order_id', merch_order_id),
                          ('organizer_id', organizer_id)):
        if value:
            return column, value
    return None, None


def _recover_abandoned_sentinel(sb, *, order_type, registration_id,
                                merch_order_id, organizer_id,
                                amount, currency) -> tuple[int, dict] | None:
    """063/064 in_progress-branch recovery (PR#23 CodeRabbit Major).

    Ordered per the 064 runbook:
      failed lookup (timeout/5xx)    → keep the sentinel; None ⇒ friendly 409;
      sentinel younger than 2 min    → live create still in flight; 409;
      zero candidates                → guarded delete + ONE recursive re-run;
      candidates ALL fail identity   → 409, sentinel KEPT (deleting it
        would strand a real pending PR); manual review;
      one candidate (strict identity)→ adopt: pending reuses the
        checkout URL (M1 shape), completed runs the webhook's idempotent
        confirm_payment and responds with the finalized shape (or, on
        confirm failure, keeps the row pending for reconcile/webhook);
      multiple candidates            → 409 + loud ops log, NO adoption.

    Candidate scoping (docs verified): HitPay's list endpoint supports
    `search` (partial match on email/id/reference_number) and
    `metadata[key]` (exact value, first pair only). The strict matcher
    below is authoritative: a PR whose reference_number or metadata does
    not name THIS exact target, or whose amount/currency disagree, is
    never adopted (no cross-tenant leakage).
    """
    column, value = _split_target(registration_id, merch_order_id, organizer_id)
    if column is None:
        return None
    target = str(value)

    sentinel_rows = (sb.table('payments').select('*')
                     .eq('order_type', order_type)
                     .eq(column, value)
                     .eq('status', 'pending')
                     .like('provider_request_id', 'pending:%')
                     .limit(1).execute())
    sentinels = sentinel_rows.data or []
    if not sentinels:
        # Recovered concurrently between the claim and here; the next
        # attempt runs the normal fresh flow.
        return None
    sentinel = str(sentinels[0].get('provider_request_id'))

    created_at = sentinels[0].get('created_at')
    if created_at:
        try:
            created = datetime.fromisoformat(str(created_at))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created < SENTINEL_RECOVER_AFTER:
                logger.info(
                    'sentinel recovery: %s (%s) is seconds old; assuming a '
                    'live in-flight create and waiting', order_type, sentinel)
                return None
        except (ValueError, TypeError):
            logger.warning('sentinel recovery: unparseable created_at on %s; '
                           'keeping it', sentinel)
            return None

    reference = str(target) if order_type != 'organizer_subscription' else None
    try:
        # `search` is docs-supported for registration/merch (their identity
        # field IS the reference_number); subscriptions carry the metadata
        # pair instead — an unambiguous global key for this one target.
        params = ({'search': reference} if reference
                  else {SENTINEL_METADATA_KEY: f'{order_type}:{target}'})
        raw = list_payment_requests(params)
    except Exception:
        logger.warning(
            'sentinel recovery: HitPay PR lookup failed for order_type=%s; '
            'keeping sentinel (no delete/adopt on uncertainty)',
            order_type, exc_info=True)
        return None

    candidates = list(raw) if isinstance(raw, list) else []

    # (b) Exclude ids already linked on a local row (sentinels excluded —
    # a linked real PR is already owned by a live row somewhere).
    linked_rows = (sb.table('payments').select('provider_request_id')
                   .eq('order_type', order_type)
                   .eq(column, value)
                   .not_.like('provider_request_id', 'pending:%')
                   .execute())
    linked = {str(r.get('provider_request_id'))
              for r in (linked_rows.data or []) if r.get('provider_request_id')}
    candidates = [c for c in candidates if str(c.get('id')) not in linked]

    # Strict identity: this exact target + matching amount/currency.
    # Unrelated partial-search results (not naming this target) are NOT
    # candidates: they never block the no-candidate recovery path.
    expected_metadata = f'{order_type}:{target}'
    matches = []
    target_named_mismatches = []
    for c in candidates:
        metadata = c.get('metadata') if isinstance(c.get('metadata'), dict) else {}
        names_target = (str(c.get('reference_number') or '') == target
                        or metadata.get('pelikat_order') == expected_metadata)
        if not names_target:
            continue
        try:
            if abs(Decimal(str(c.get('amount'))) - Decimal(str(amount))) > Decimal('0.01'):
                target_named_mismatches.append(c)
                continue
        except (InvalidOperation, TypeError):
            target_named_mismatches.append(c)
            continue
        if str(c.get('currency') or 'MYR').upper() != currency:
            target_named_mismatches.append(c)
            continue
        matches.append(c)

    if len(matches) > 1:
        ids = [str(m.get('id')) for m in matches]
        logger.error(
            'sentinel recovery: %d orphan PRs ambiguous for order_type=%s '
            'target=%s (ids=%s); refusing blind adoption — ops must '
            'resolve manually', len(matches), order_type, sentinel, ids)
        return 409, {'error': 'Checkout already initializing; retry in a '
                              'few seconds'}

    if not matches and target_named_mismatches:
        # A target-NAMING candidate FAILED the strict identity match:
        # deleting the sentinel and re-creating would strand a REAL pending
        # PR at HitPay that no local row references (webhook/confirm
        # null-match, charge unrecordable). Leave the pair untouched for
        # ops review. Unrelated search noise does NOT hit this branch.
        details = ''
        for c in target_named_mismatches:
            meta = c.get('metadata')
            pelikat_order = (meta or {}).get('pelikat_order') \
                if isinstance(meta, dict) else None
            details += (f"id={c.get('id')} actual(amount={c.get('amount')}, "
                        f"currency={c.get('currency')}, "
                        f"reference_number={c.get('reference_number')}, "
                        f"metadata={pelikat_order}); ")
        logger.error(
            'sentinel recovery: candidate PR(s) for order_type=%s target=%s '
            'FAILED strict identity check against sentinel %s '
            '(expected amount=%s currency=%s reference=%s) — sentinel kept, '
            'NO re-create; manual review required. Candidates: %s',
            order_type, target, sentinel, amount, currency, target, details)
        return 409, {'error': 'Payment initialization requires manual '
                     'review; contact support or try a fresh '
                     'registration later.'}

    # No candidates → sentinel is a verified-before-create crash: signal
    # the caller (the claim's owner) to run the fresh flow once more.
    if not matches:
        # Guarded delete: the target row must STILL be a `pending:...`
        # sentinel at the moment of deletion (someone could have adopted
        # it concurrently — that update would have swapped the id, so the
        # .like guard matches 0 rows and no harm is done).
        sb.table('payments').delete() \
            .eq('provider_request_id', sentinel) \
            .like('provider_request_id', 'pending:%').execute()
        logger.info(
            'sentinel recovery: no orphan PR found; released crashed '
            'sentinel %s (order_type=%s) — one fresh flow re-run follows',
            sentinel, order_type)
        return _RECOVERY_RETRY

    # ONE candidate → adopt it.
    match = matches[0]
    real_id = str(match['id'])
    pr_status = str(match.get('status') or 'pending')
    qr_data = match.get('qr_code_data')
    qr_code = qr_data.get('qr_code') if isinstance(qr_data, dict) else None

    try:
        # Persist ONLY the real PR linkage (+ QR snapshot); KEEP
        # status='pending' until confirm_payment succeeds — an exception
        # mid-way must never leave a non-pending row that no
        # webhook/redelivery/reconcile path can finalize.
        sb.table('payments').update({
            'provider_request_id': real_id,
            'provider': 'hitpay',
            'qr_code_data': {'qr_code': qr_code} if qr_code else None,
        }).eq('provider_request_id', sentinel).execute()
    except Exception:
        logger.exception(
            'sentinel recovery: adoption update failed for sentinel %s → '
            '%s; caller retries normally', sentinel, real_id)
        return None

    # Adoption lost the race? (someone else swapped the sentinel in
    # between). A verify-select naming the real id resolves it.
    verify = (sb.table('payments').select('provider_request_id')
              .eq('provider_request_id', real_id).limit(1).execute())
    if not (verify.data or []):
        logger.warning('sentinel recovery: adoption of %s did not take '
                       'eff(verified 0 rows) — caller will re-claim', real_id)
        return None

    if pr_status == 'completed':
        # The money likely already moved: run the webhook's own idempotent
        # confirm path (the same rpc confirm_payment the verified event
        # handler uses) with the PR's succeeded sub-payment.
        payments_list = match.get('payments') or []
        succeeded = next((p for p in payments_list
                          if p.get('status') == 'succeeded'), None)
        confirm_ok = True
        try:
            sb.rpc('confirm_payment', {
                'p_provider_request_id': real_id,
                'p_provider_payment_id': succeeded.get('id') if succeeded else None,
                'p_rail': succeeded.get('payment_type') if succeeded else None,
            }).execute()
        except Exception:
            # NOT finalized: leave the row pending with the real PR id —
            # webhook redelivery / reconcile (which polls pending rows)
            # re-runs the idempotent confirm and closes it later. Do NOT
            # surface completion to the caller yet.
            confirm_ok = False
            logger.exception('sentinel recovery: confirm_payment failed '
                             'for %s; row left pending for '
                             'reconcile/webhook redelivery', real_id)
        if confirm_ok:
            logger.info('sentinel recovery: adopted COMPLETED PR %s for %s '
                        '— finalized via confirm_payment', real_id, order_type)
            direct_link = match.get('direct_link') or {}
            return 200, {
                'provider_request_id': real_id,
                'checkout_url': match.get('url'),
                'qr_code': qr_code,
                'direct_link_url': direct_link.get('direct_link_url'),
                'status': match.get('status', pr_status),
            }
        logger.info('sentinel recovery: adopted COMPLETED PR %s for %s — '
                    'confirmation in progress (row stays pending)',
                    real_id, order_type)
        return 200, {
            'provider_request_id': real_id,
            'checkout_url': match.get('url'),
            'qr_code': qr_code,
            'direct_link_url': (match.get('direct_link') or {})
                .get('direct_link_url'),
            'status': 'pending',
            'message': 'Payment received but confirmation is in progress',
        }
    # M1-compatible reuse shape (drives the normal GET /payments/status /
    # follow flow); the pending PR's own truth is carried in the body.
    return 200, {
        'provider_request_id': real_id,
        'checkout_url': match.get('url'),
        'qr_code': qr_code,
        'direct_link_url': (match.get('direct_link') or {})
            .get('direct_link_url'),
        'status': match.get('status', pr_status),
    }


# Sentinel-recovery protocol values for _recover_abandoned_sentinel's
# return: a (status, body) tuple = adopted orphan; _RECOVERY_RETRY = the
# dead sentinel was deleted, run the fresh flow once more; anything else
# (None) = keep the default 063 friendly 409 (live create / uncertainty).
_RECOVERY_RETRY = object()

def create_payment(data: dict, _attempt: int = 0) -> tuple[int, dict]:
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
    if claim_row and claim_row.get('in_progress'):
        # 063 tri-state, extended per the 064 runbook: try SAFE recovery of
        # a likely-crashed sentinel before the friendly 409 (in-flight
        # lookups that fail or are too young keep returning 409 exactly as
        # before). Recovery returns a (status, body) response tuple, or
        # None for "keep the default 409 / continue fresh flow".
        recovery = _recover_abandoned_sentinel(
            sb, order_type=order_type, registration_id=registration_id,
            merch_order_id=merch_order_id, organizer_id=organizer_id,
            amount=amount, currency=currency)
        if isinstance(recovery, tuple):
            return recovery  # adopted a pending/completed orphan PR
        if recovery is _RECOVERY_RETRY:
            # Verified-dead sentinel was deleted: run the normal fresh
            # flow exactly once more (depth-1 recursion guard).
            if _attempt >= 1:
                return 409, {'error': 'Could not reserve a payment slot; '
                                      'try again'}
            return create_payment(data, _attempt=_attempt + 1)
        # in-flight live create or lookup uncertainty: unchanged 063 409.
        return 409, {
            'error': 'Checkout already initializing; retry in a few seconds',
        }

    if not claim_row or not claim_row.get('provider_request_id'):
        logger.error('claim_payment_slot returned no slot (order_type=%s)',
                     order_type)
        return 409, {'error': 'Could not reserve a payment slot; try again'}

    provider_request_id = str(claim_row['provider_request_id'])

    if not claim_row.get('is_reservation'):
        # M1: a pending PR already exists for this order — reuse it ONLY
        # when its stored amount still matches the amount just resolved
        # server-side (compare at 2dp, same tolerance as sentinel
        # recovery). A price change (e.g. admin edits the organizer
        # subscription price) otherwise keeps serving the STALE checkout
        # amount until the pending row ages out. On mismatch: release the
        # row ('expired' — a terminal status in 059's payments check, the
        # same convention reconcile uses) and re-claim fresh so a new PR
        # is created at the new amount. The superseded remote PR is then
        # cancelled best-effort (delete_payment_request) so the old
        # checkout URL cannot still be paid into an expired row; any
        # remote cancellation error is non-fatal — a late completion is
        # left to the webhook, which accepts it on an expired row (the
        # customer paid a price they saw).
        existing = (sb.table('payments').select('*')
                    .eq('provider_request_id', provider_request_id)
                    .limit(1).execute())
        rows = existing.data or []
        if rows:
            try:
                same_amount = abs(Decimal(str(rows[0].get('amount')))
                                  - Decimal(str(amount))) <= Decimal('0.01')
            except (InvalidOperation, TypeError):
                same_amount = False
            if same_amount:
                return _reuse_pending_payment(rows[0])
            logger.warning(
                'stale pending payment %s (amount=%s) mismatches current '
                'price %s for order_type=%s — releasing it and creating a '
                'fresh payment at the new amount',
                provider_request_id, rows[0].get('amount'), amount,
                order_type)
            # Guarded release: the row must STILL be pending at this id
            # (someone confirming/cancelling it concurrently makes the
            # update match 0 rows; the re-claim below then behaves
            # correctly either way).
            sb.table('payments').update({'status': 'expired'}) \
                .eq('provider_request_id', provider_request_id) \
                .eq('status', 'pending').execute()
            # Local row is safely terminal — only now cancel the remote PR
            # (best-effort). HitPay answers 4xx/409/404 for PRs it will not
            # void (already completed etc.) and can time out; either way
            # the flow MUST proceed to the fresh claim — the webhook owns
            # the "someone paid anyway" case.
            try:
                delete_payment_request(provider_request_id)
            except Exception:
                logger.warning(
                    'could not cancel superseded HitPay PR %s after '
                    'amount-mismatch release (left to the webhook)',
                    provider_request_id, exc_info=True)
            if _attempt >= 1:
                return 409, {'error': 'Could not reserve a payment slot; '
                                      'try again'}
            return create_payment(data, _attempt=_attempt + 1)
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
    # Recovery scope key: one metadata pair naming order_type + target id,
    # so GET /payment-requests can filter an orphaned PR back to THIS
    # logical order (organizer_subscription has no reference_number at
    # all). Non-secret, well within HitPay's metadata limits (≤50 keys/
    # ≤500-char values).
    _scope_target = registration_id or merch_order_id or organizer_id
    if _scope_target:
        payload[SENTINEL_METADATA_KEY] = f'{order_type}:{_scope_target}'
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

def _client_amount_prevalidated(amount: str | None) -> str | None:
    """Numeric/positive-only pre-validation of a client refund amount (the
    remaining-bound check needs the post-claim fresh row, so it happens
    later). Returns constant error text or None when acceptable."""
    if amount is None:
        return None
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        return 'Refund amount must be a number'
    if not value.is_finite() or value <= 0:
        return 'Refund amount must be greater than zero'
    return None


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

    # M3 pre-validation before claiming: the client amount must be numeric,
    # finite and > 0. The *remaining-bound* check needs a fresh refunds
    # read, so it happens after the claim below.
    amount_error = _client_amount_prevalidated(amount)
    if amount_error:
        return 422, {'error': amount_error}

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

    # CodeRabbit Major (refund race): the validation read must happen AFTER
    # winning the claim — a concurrent winner may already have bumped
    # refunded_amount before this request claimed the row. Re-fetch the
    # freshly-claimed row and validate against *that* value.
    fresh = (sb.table('payments').select('*')
             .eq('provider_request_id', provider_request_id)
             .limit(1).execute())
    fresh_rows = fresh.data if isinstance(fresh.data, list) else []
    payment = fresh_rows[0] if fresh_rows else payment
    refund_amount, amount_error = _validated_refund_amount(
        amount, payment['amount'], payment.get('refunded_amount'))
    if amount_error or refund_amount is None:
        # Validation failed post-claim — release the claim (inverse of the
        # guarded update above) and never forward to HitPay.
        sb.table('payments').update({'refund_blocked_reason': None}) \
            .eq('provider_request_id', provider_request_id) \
            .eq('refund_blocked_reason', 'refund_in_progress').execute()
        return 422, {'error': amount_error or 'Invalid refund amount'}

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
    # Null-guard: the execute() result itself may be None (unknown) — treat
    # as an empty read → conservative no-op. Also no row (or non-dict
    # payload) → can't verify age.
    result = (sb.table('webhook_events').select('received_at')
              .eq('payload_hash', claim_hash)
              .maybe_single().execute())
    data = getattr(result, 'data', None)
    data = data if isinstance(data, dict) else {}
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
