# Pelikat API

> Django backend for the Pelikat platform: AI photo tagging, document encryption (IC/MRZ parsing), HMAC-signed QR security, e-cert generation, badge evaluation, and HitPay payment processing.

## Stack

- Django 5
- DRF (Django REST Framework)
- Supabase (via supabase-py, psycopg2)
- YOLOv8 (ultralytics) + PaddleOCR
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

## Tests

```bash
pytest
```

Uses `pytest.ini` (settings module `pelikat.settings`). Test suites cover the AI pipeline, multi-tenancy, payments/webhooks, document/IC parsing rules, QR signing, idempotency, middleware, and performance (marked `performance`, `security`, etc. — see `pytest.ini`).

## Environment Variables

All variable names/purposes (see `.env.example`; values are never committed):

- `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_JWT_SECRET` — Supabase connection and JWT verification.
- `HMAC_SECRET` — HMAC signing of QR payloads.
- `INTERNAL_API_KEY` — shared secret required on `/ai/*` and `/payments/*` requests (Next.js ↔ API).
- `DOCUMENT_ENCRYPTION_KEY` — key for document encryption endpoints.
- `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` — standard Django settings.
- `AI_PHOTOS_*` (YOLO/OCR model path, confidence, IoU, upscale, crop padding, auto/review confidence thresholds) — photo pipeline tuning.
- `HITPAY_API_URL`, `HITPAY_API_KEY`, `HITPAY_WEBHOOK_SALT`, `HITPAY_PAYMENT_METHODS` — HitPay payment gateway (sandbox vs production base URL, webhook salt).

## Endpoints

All `/ai/*` and `/payments/*` endpoints require the `X-Internal-Key` header matching `INTERNAL_API_KEY` (validated by middleware in `pelikat/middleware.py`).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ai/photos/read` | POST | OCR photos uploaded directly |
| `/ai/photos/read-storage` | POST | OCR photos from Supabase storage |
| `/ai/photos/process` | POST | Run YOLO → OCR → write `photo_tags` |
| `/ai/photos/process-prefix` | POST | Process photos by storage path prefix |
| `/ai/photos/status/<batch_id>` | GET | Get processing status for a batch |
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
    ├── ai_photos/           # YOLO + PaddleOCR photo pipeline
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
