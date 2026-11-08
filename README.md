# Pelikat API

> Django backend for the Pelikat platform: AI photo tagging, document encryption (IC/MRZ parsing), HMAC-signed QR security, e-cert generation, badge evaluation, and HitPay payment processing.

> **Staging branch?** The `staging` branch runs the same pipeline on a
> zero-AVX CPU runtime (ONNX Runtime + RapidOCR) for the constrained staging
> VPS. See [STAGING_DIFFERENCES.md](STAGING_DIFFERENCES.md).

## Stack

- Django 5
- DRF (Django REST Framework)
- Supabase (via supabase-py, psycopg2)
- YOLO bib detector on ONNX Runtime + RapidOCR (PP-OCRv5 mobile ONNX, bundled models)
- Pillow
- cryptography (document encryption)
- HitPay payment gateway integration

## Quick Start

### Local Development

```bash
# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy environment file
cp .env.example .env
# Edit .env with your credentials (see Environment Variables below)

# 4. Run server
python manage.py runserver 0.0.0.0:8000
```

### Docker

#### Development (hot reload)

```bash
docker-compose -f docker-compose.dev.yml up --build
```

#### Production

```bash
docker-compose up --build
```

Both compose files start two services: `api` (gunicorn / runserver) and
`photo-worker` (`python manage.py run_photo_worker`), which processes queued
photo batches in the background. Run exactly one `photo-worker`. It needs
migrations `087_photo_batches_queue` and `088_photo_tags_review_provenance`
(pelikat-web) applied.

## Tests

```bash
pytest
```

Uses `pytest.ini` (settings module `pelikat.settings`). Test suites cover the AI pipeline, multi-tenancy, payments/webhooks, document/IC parsing rules, QR signing, idempotency, middleware, and performance (marked `performance`, `security`, etc. — see `pytest.ini`).

## Environment Variables

All variable names/purposes (see `.env.example`; values are never committed):

- `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` — Supabase connection.
- `HMAC_SECRET` — HMAC signing of QR payloads.
- `INTERNAL_API_KEY` — shared secret required on `/ai/*` and `/payments/*` requests (Next.js ↔ API).
- `DOCUMENT_ENCRYPTION_KEY` — key for document encryption endpoints.
- `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` — standard Django settings.
- `AI_PHOTOS_*` (YOLO/OCR model paths, confidence, IoU, crop padding, auto/review confidence thresholds) — photo pipeline tuning.
- `HITPAY_API_URL`, `HITPAY_API_KEY`, `HITPAY_WEBHOOK_SALT`, `HITPAY_PAYMENT_METHODS` — HitPay payment gateway (sandbox vs production base URL, webhook salt).

## Endpoints

All `/ai/*` and `/payments/*` endpoints require the `X-Internal-Key` header matching `INTERNAL_API_KEY` (validated by middleware in `pelikat/middleware.py`), except the HitPay webhook, which is signature-verified.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ai/photos/read` | POST | OCR photos uploaded directly |
| `/ai/photos/read-storage` | POST | OCR photos from Supabase storage |
| `/ai/photos/process` | POST | Queue photos for the worker (YOLO → OCR → `photo_tags`); returns 202 + batch |
| `/ai/photos/process-prefix` | POST | Queue every photo under a storage prefix as a reprocess batch; returns 202 |
| `/ai/photos/status/<batch_id>` | GET | Batch status and per-photo counts (auto-tagged, flagged, no bib, failed) |
| `/ai/qr/sign` | POST | Generate HMAC-SHA256 signed QR |
| `/ai/qr/verify` | POST | Verify scanned QR payload |
| `/ai/qr/consent-code` | POST | Consent code endpoint |
| `/ai/ecert/generate` | POST | Generate e-cert PNG |
| `/ai/badges/evaluate` | POST | Evaluate badges |
| `/ai/badges/definitions` | GET | Get badge definitions |
| `/ai/documents/encrypt` | POST | Encrypt a document |
| `/ai/documents/encrypt-ic` | POST | Encrypt IC (MRZ/NRIC parsing) |
| `/ai/documents/delete` | POST | Delete encrypted documents |
| `/payments/create` | POST | Create a HitPay payment |
| `/payments/webhook/hitpay/` | POST | HitPay webhook (signature-verified) |
| `/payments/status` | GET | Payment status check |
| `/payments/refund` | POST | Refund a payment |

### Payment reconcile cron

`apps/payments/management/commands/reconcile_payments.py` polls HitPay for pending payments older than 15 minutes. Schedule it (cron) as:

```
*/15 * * * * cd /app && python manage.py reconcile_payments
```

## Project Structure

```
pelikat-api/
├── manage.py
├── pytest.ini
├── requirements.txt
├── Dockerfile               # Production
├── Dockerfile.dev           # Development
├── Dockerfile.prod          # Production (alt)
├── docker-compose.dev.yml   # Development
├── docker-compose.yml       # Production
├── .env.example
├── models/
│   └── best.pt              # YOLO weights
├── pelikat/
│   ├── settings.py
│   ├── urls.py
│   ├── exceptions.py        # Global DRF exception handler
│   ├── wsgi.py
│   └── middleware.py        # Internal API key validation
└── apps/
    ├── ai_photos/           # YOLO (ONNX) + RapidOCR photo pipeline
    ├── documents/           # IC/document encryption, MRZ & NRIC parsers
    ├── qr_security/         # HMAC sign/verify + consent codes
    ├── ecert/               # E-cert generation
    ├── badges/              # Badge definitions & evaluation
    ├── payments/            # HitPay integration + reconcile command
    └── tests/               # pytest suite (unit/security/performance)
```

## Docs

See `docs/` for module descriptions (`modules_and_features.md`) and specs (`spec_django.md`, `spec_nextjs.md`, `spec_supabase.md`).

## Security

- All `/ai/*` endpoints require the `X-Internal-Key` header
- Middleware blocks unauthorized requests
- HitPay webhooks are signature-verified
- Use `SUPABASE_SERVICE_ROLE_KEY` only server-side (never expose to frontend)

<!-- TODO: deployment target (hosting, storage, monitoring) is not documented in the repo — add a Deployment section once decided. -->
