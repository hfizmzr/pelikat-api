# Pelikat — Django Spec & Guide

> **Scope:** Django is a lean AI worker + security utility. 5 endpoints total. Nothing else.  
> **Stack:** Django 5 · DRF · PostgreSQL (via psycopg2) · YOLOv8 · PaddleOCR · Pillow · Celery (optional for async)

---

## 1. Django's Role (Strict Boundaries)

Django does **NOT** replace Supabase PostgREST. It only handles:

| # | Endpoint | What it does |
|---|----------|-------------|
| 1 | `POST /ai/photos/process` | Receive upload notification → run YOLO → OCR → write `photo_tags` |
| 2 | `GET  /ai/photos/status/{batch_id}` | Return processing status for a batch |
| 3 | `POST /ai/qr/sign` | Generate HMAC-SHA256 signed QR payload |
| 4 | `POST /ai/qr/verify` | Verify a scanned QR payload server-side |
| 5 | `POST /ai/ecert/generate` | Generate registration e-cert PDF (Pillow) |

> All endpoints are **internal-only** (called by Next.js server-side or Supabase Edge Functions). They are **not** called by the browser directly.

---

## 2. Project Structure

```
pelikat-api/
├── manage.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env
├── pelikat/                    # Django project settings
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── apps/
    ├── ai_photos/              # YOLO + OCR pipeline
    │   ├── views.py
    │   ├── services.py         # Pipeline logic
    │   └── urls.py
    ├── qr_security/            # HMAC sign / verify
    │   ├── views.py
    │   ├── services.py
    │   └── urls.py
    └── ecert/                  # E-cert PDF generation
        ├── views.py
        ├── services.py
        └── urls.py
```

---

## 3. Setup & Dependencies

### 3.1 requirements.txt

```txt
django>=5.0
djangorestframework>=3.15
psycopg2-binary>=2.9
supabase>=2.0         # Python Supabase client
python-dotenv>=1.0
Pillow>=10.0          # E-cert PDF / image generation
ultralytics>=8.0      # YOLOv8
paddleocr>=2.7        # PaddleOCR (or use easyocr as lighter alternative)
celery>=5.3           # Async task queue (optional, use if photo processing is slow)
redis>=5.0            # Celery broker (optional)
```

> **Simpler OCR alternative:** If PaddleOCR feels heavy, replace with `easyocr` — same API, easier install.

### 3.2 settings.py (key sections)

```python
import os
from dotenv import load_dotenv

load_dotenv()

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'apps.ai_photos',
    'apps.qr_security',
    'apps.ecert',
]

# No Django ORM models for Supabase tables — we talk via supabase-py directly
DATABASES = {}  # leave empty OR point to Supabase direct connection for admin use

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],  # JWT verified manually
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny'],
}

# Secrets
HMAC_SECRET = os.environ['HMAC_SECRET']           # random 32-byte hex string
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_SERVICE_ROLE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
SUPABASE_JWT_SECRET = os.environ['SUPABASE_JWT_SECRET']

# Internal API key (Next.js → Django calls must include this header)
INTERNAL_API_KEY = os.environ['INTERNAL_API_KEY']
```

### 3.3 .env

```env
HMAC_SECRET=your-random-32-byte-hex-string
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SUPABASE_JWT_SECRET=your-supabase-jwt-secret
INTERNAL_API_KEY=a-random-shared-secret-for-nextjs-to-django
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
```

---

## 4. Security: Internal API Key Middleware

All endpoints require an `X-Internal-Key` header to prevent public access:

```python
# pelikat/middleware.py
from django.conf import settings
from django.http import JsonResponse

class InternalKeyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith('/ai/'):
            key = request.headers.get('X-Internal-Key', '')
            if key != settings.INTERNAL_API_KEY:
                return JsonResponse({'error': 'Unauthorized'}, status=401)
        return self.get_response(request)
```

Add to `MIDDLEWARE` in settings.py.

---

## 5. Endpoint Implementations

### 5.1 AI Photo Pipeline

**`POST /ai/photos/process`**

```
Request body:
{
  "batch_id": "uuid",
  "event_id": "uuid",
  "organizer_id": "uuid",
  "storage_paths": ["race-photos/event123/img001.jpg", ...]
}

Response:
{ "batch_id": "uuid", "queued": 12 }
```

```python
# apps/ai_photos/services.py
from ultralytics import YOLO
import easyocr  # or paddleocr
from supabase import create_client
from django.conf import settings

model = YOLO('yolov8n.pt')          # load once at module level
reader = easyocr.Reader(['en'])     # load once

supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

def process_photo(storage_path, event_id, organizer_id, batch_id):
    """Download photo, run YOLO → crop bib region → OCR → write photo_tags."""
    # 1. Download from Supabase Storage
    res = supabase.storage.from_('race-photos').download(storage_path)
    img_bytes = res

    # 2. YOLO inference — detect bib region
    import numpy as np, cv2
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    results = model(img)

    bib_number = None
    confidence = 0.0

    for box in results[0].boxes:
        if box.conf[0] > 0.5:                  # confidence threshold
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            crop = img[y1:y2, x1:x2]
            
            # 3. OCR on cropped region
            ocr_result = reader.readtext(crop)
            if ocr_result:
                text = ocr_result[0][1].strip().upper()
                conf = float(ocr_result[0][2])
                if conf > confidence:
                    bib_number = text
                    confidence = conf

    # 4. Determine status
    if confidence > 0.85:
        status = 'auto'
    elif confidence >= 0.50:
        status = 'review'
    else:
        status = 'discarded'

    # 5. Write to photo_tags (using service_role → bypasses RLS)
    supabase.table('photo_tags').insert({
        'event_id': event_id,
        'organizer_id': organizer_id,
        'storage_path': storage_path,
        'bib_number': bib_number,
        'confidence': confidence,
        'status': status,
        'batch_id': batch_id,
    }).execute()
```

```python
# apps/ai_photos/views.py
from rest_framework.decorators import api_view
from rest_framework.response import Response
import threading
from .services import process_photo

@api_view(['POST'])
def process_photos(request):
    data = request.data
    batch_id = data['batch_id']
    event_id = data['event_id']
    organizer_id = data['organizer_id']
    paths = data['storage_paths']

    # Run in background threads (simple, no Celery needed for FYP)
    for path in paths:
        t = threading.Thread(
            target=process_photo,
            args=(path, event_id, organizer_id, batch_id)
        )
        t.daemon = True
        t.start()

    return Response({'batch_id': batch_id, 'queued': len(paths)})

@api_view(['GET'])
def photo_status(request, batch_id):
    from supabase import create_client
    from django.conf import settings
    sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    
    res = sb.table('photo_tags') \
            .select('status') \
            .eq('batch_id', batch_id) \
            .execute()
    
    counts = {}
    for row in res.data:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    
    return Response({'batch_id': batch_id, 'counts': counts})
```

---

### 5.2 HMAC QR Sign / Verify

**`POST /ai/qr/sign`**

```
Request: { "runner_id": "uuid", "event_id": "uuid", "bib_number": "A001" }
Response: { "qr_payload": "base64-encoded-signed-string" }
```

**`POST /ai/qr/verify`**

```
Request: { "qr_payload": "base64-encoded-signed-string" }
Response: { "valid": true, "runner_id": "uuid", "event_id": "uuid", "bib_number": "A001" }
```

```python
# apps/qr_security/services.py
import hmac, hashlib, json, base64, time
from django.conf import settings

def sign_qr(runner_id: str, event_id: str, bib_number: str) -> str:
    payload = {
        'runner_id': runner_id,
        'event_id': event_id,
        'bib_number': bib_number,
        'ts': int(time.time()),         # timestamp to detect old QRs
    }
    payload_json = json.dumps(payload, separators=(',', ':'))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
    
    sig = hmac.new(
        settings.HMAC_SECRET.encode(),
        payload_b64.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return f"{payload_b64}.{sig}"

def verify_qr(qr_payload: str) -> dict:
    try:
        payload_b64, sig = qr_payload.rsplit('.', 1)
        expected_sig = hmac.new(
            settings.HMAC_SECRET.encode(),
            payload_b64.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(sig, expected_sig):
            return {'valid': False, 'error': 'Invalid signature'}
        
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
        
        # Optional: reject QRs older than 1 year (for static BIB QRs, keep long TTL)
        # if time.time() - payload['ts'] > 86400:
        #     return {'valid': False, 'error': 'QR expired'}
        
        return {'valid': True, **payload}
    except Exception as e:
        return {'valid': False, 'error': str(e)}
```

```python
# apps/qr_security/views.py
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .services import sign_qr, verify_qr

@api_view(['POST'])
def qr_sign(request):
    data = request.data
    token = sign_qr(data['runner_id'], data['event_id'], data['bib_number'])
    return Response({'qr_payload': token})

@api_view(['POST'])
def qr_verify(request):
    result = verify_qr(request.data.get('qr_payload', ''))
    return Response(result)
```

---

### 5.3 E-Cert Generation

**`POST /ai/ecert/generate`**

```
Request: { "runner_id": "uuid", "event_id": "uuid", "registration_id": "uuid" }
Response: { "cert_url": "https://...supabase.co/storage/v1/object/sign/..." }
```

```python
# apps/ecert/services.py
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client
from django.conf import settings
import io, os

sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

def generate_cert(runner_name: str, event_name: str, bib_number: str, 
                  event_date: str, registration_id: str) -> str:
    """Generate PNG e-cert, upload to Supabase Storage, return signed URL."""
    
    # 1. Load template (store a cert_template.png in your repo)
    template_path = os.path.join(os.path.dirname(__file__), 'cert_template.png')
    img = Image.open(template_path).convert('RGB')
    draw = ImageDraw.Draw(img)
    
    # 2. Add text (adjust coordinates to match your template)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 60)
    draw.text((img.width // 2, 500), runner_name, fill='black', font=font, anchor='mm')
    draw.text((img.width // 2, 620), event_name,  fill='#555', font=font, anchor='mm')
    draw.text((img.width // 2, 720), bib_number,  fill='#888', font=font, anchor='mm')
    draw.text((img.width // 2, 820), event_date,  fill='#888', font=font, anchor='mm')
    
    # 3. Save to bytes buffer
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    
    # 4. Upload to Supabase Storage
    storage_path = f"certs/{registration_id}.png"
    sb.storage.from_('certificates').upload(
        path=storage_path,
        file=buf.read(),
        file_options={'content-type': 'image/png', 'upsert': 'true'}
    )
    
    # 5. Return a signed URL (24h)
    res = sb.storage.from_('certificates').create_signed_url(storage_path, 86400)
    return res['signedURL']
```

---

## 6. URL Config

```python
# pelikat/urls.py
from django.urls import path, include

urlpatterns = [
    path('ai/photos/', include('apps.ai_photos.urls')),
    path('ai/qr/',     include('apps.qr_security.urls')),
    path('ai/ecert/',  include('apps.ecert.urls')),
]

# apps/ai_photos/urls.py
from django.urls import path
from . import views
urlpatterns = [
    path('process',         views.process_photos),
    path('status/<batch_id>', views.photo_status),
]

# apps/qr_security/urls.py
from django.urls import path
from . import views
urlpatterns = [
    path('sign',   views.qr_sign),
    path('verify', views.qr_verify),
]

# apps/ecert/urls.py
from django.urls import path
from . import views
urlpatterns = [
    path('generate', views.generate_cert_view),
]
```

---

## 7. Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libgl1 libglib2.0-0 libgomp1 \
    libfreetype6-dev fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
```

---

## 8. Environment Variables (Django .env)

```env
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1,pelikat-api

HMAC_SECRET=your-random-32-byte-hex-string-here
INTERNAL_API_KEY=shared-secret-nextjs-uses-to-call-django

SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SUPABASE_JWT_SECRET=your-supabase-jwt-secret
```

---

## 9. Consent Code Generation (bonus endpoint — or Supabase Edge Function)

> Generate a 6-character alphanumeric proxy collection code.  
> Can be a simple Django view OR a Supabase Edge Function (Deno). Keep in Django since HMAC_SECRET is already there.

```python
# In apps/qr_security/services.py — add this function
import secrets, string

def generate_consent_code() -> str:
    """Returns a 6-char uppercase alphanumeric code, URL-safe."""
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(6))
```

Called by Next.js server action → Django → writes to `repc_consent_codes` via Supabase service_role.

---

## 10. Development Tips for FYP

- Use **threading** (not Celery) for photo processing — much simpler for FYP scope.
- Use **easyocr** instead of PaddleOCR — easier to install on Docker/WSL.
- You can skip GPU and run inference on CPU — slower but works fine for demo.
- Test endpoints with **Postman** or **httpie**.
- Mount a volume in Docker so code changes reflect without rebuild (`-v ./:/app`).
