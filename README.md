# Pelikat API

> Django is a lean AI worker + security utility. 5 endpoints total.

## Stack

- Django 5
- DRF (Django REST Framework)
- Supabase (via supabase-py)
- YOLOv8 + EasyOCR
- Pillow

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
# Edit .env with your Supabase credentials

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

## Environment Variables

```env
# Required
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SUPABASE_JWT_SECRET=your-jwt-secret
HMAC_SECRET=your-random-32-byte-hex-string
INTERNAL_API_KEY=shared-secret-for-nextjs

# Optional
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
SECRET_KEY=change-this-in-production
```

## Endpoints

All endpoints require `X-Internal-Key` header matching `INTERNAL_API_KEY`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ai/photos/process` | POST | Run YOLO → OCR → write `photo_tags` |
| `/ai/photos/status/<batch_id>` | GET | Get processing status for a batch |
| `/ai/qr/sign` | POST | Generate HMAC-SHA256 signed QR |
| `/ai/qr/verify` | POST | Verify scanned QR payload |
| `/ai/ecert/generate` | POST | Generate e-cert PNG |

## API Usage

### Photo Processing

```bash
curl -X POST http://localhost:8000/ai/photos/process \
  -H "Content-Type: application/json" \
  -H "X-Internal-Key: your-internal-key" \
  -d '{
    "batch_id": "uuid",
    "event_id": "uuid",
    "organizer_id": "uuid",
    "storage_paths": ["race-photos/img001.jpg"]
  }'
```

### QR Sign

```bash
curl -X POST http://localhost:8000/ai/qr/sign \
  -H "Content-Type: application/json" \
  -H "X-Internal-Key: your-internal-key" \
  -d '{
    "runner_id": "uuid",
    "event_id": "uuid",
    "bib_number": "A001"
  }'
```

### QR Verify

```bash
curl -X POST http://localhost:8000/ai/qr/verify \
  -H "Content-Type: application/json" \
  -H "X-Internal-Key: your-internal-key" \
  -d '{"qr_payload": "base64.sig"}'
```

### E-Cert Generate

```bash
curl -X POST http://localhost:8000/ai/ecert/generate \
  -H "Content-Type: application/json" \
  -H "X-Internal-Key: your-internal-key" \
  -d '{
    "runner_name": "John Doe",
    "event_name": "KL Marathon 2025",
    "bib_number": "A001",
    "event_date": "2025-03-15",
    "registration_id": "uuid"
  }'
```

## Project Structure

```
pelikat-api/
├── manage.py
├── requirements.txt
├── Dockerfile.dev         # Development
├── Dockerfile.prod        # Production
├── docker-compose.dev.yml # Development
├── docker-compose.yml     # Production
├── .env.example
├── .gitignore
├── pelikat/
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── middleware.py    # Internal API key validation
└── apps/
    ├── ai_photos/       # YOLO + OCR
    ├── qr_security/     # HMAC sign/verify
    └── ecert/          # E-cert PDF
```

## Security

- All `/ai/*` endpoints require `X-Internal-Key` header
- Middleware blocks unauthorized requests
- Use `service_role` key only server-side (never expose to frontend)