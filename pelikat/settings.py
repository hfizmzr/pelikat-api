import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-change-this-in-production')

# M5: default OFF — an unset DEBUG env var must not enable debug mode in prod.
DEBUG = os.environ.get('DEBUG', 'False').lower() in ('1', 'true', 'yes')

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'rest_framework',
    'apps.ai_photos',
    'apps.qr_security',
    'apps.ecert',
    'apps.badges',
    'apps.documents',
    'apps.payments',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'pelikat.middleware.InternalKeyMiddleware',
]

ROOT_URLCONF = 'pelikat.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'pelikat.wsgi.application'

DATABASES = {}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.AllowAny'],
    # JSON always: even unhandled Exceptions render {"error": ...} for
    # /ai/* (and payments/*) callers — no Django HTML 500 page.
    'EXCEPTION_HANDLER': 'pelikat.exceptions.exception_handler',
    # Rates consumed by the view-level throttle classes (apps/documents).
    # ic_documents keys on the caller's user id; anon is a wider fallback
    # keyed on the request IP (all traffic arrives via the BFF host).
    'DEFAULT_THROTTLE_RATES': {
        'anon': '120/hour',
        'ic_doc_upload': '10/hour',
        'ic_doc_delete': '30/hour',
    },
}

# Multipart/body hard caps for the IC upload path (guardrails mirrored in
# apps/documents/views.py checks).
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

HMAC_SECRET = os.environ.get('HMAC_SECRET', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
SUPABASE_JWT_SECRET = os.environ.get('SUPABASE_JWT_SECRET', '')
# Fail-closed (Critical #5): an empty internal API key must never boot,
# even under DEBUG — an empty key would make the middleware pass everyone.
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY', '')
if not INTERNAL_API_KEY:
    raise ImproperlyConfigured(
        'INTERNAL_API_KEY must be set (empty keys are rejected in all modes)')
DOCUMENT_ENCRYPTION_KEY = os.environ.get('DOCUMENT_ENCRYPTION_KEY', '')
HITPAY_API_URL = os.environ.get('HITPAY_API_URL', '')
HITPAY_API_KEY = os.environ.get('HITPAY_API_KEY', '')
HITPAY_WEBHOOK_SALT = os.environ.get('HITPAY_WEBHOOK_SALT', '')
# Optional: force-enable specific HitPay rails, comma-separated
# (e.g. grabpay_direct,shopee_pay,atome). Empty = let the merchant
# Dashboard's toggled-on rails govern (payment_methods omitted from payload).
HITPAY_PAYMENT_METHODS = os.environ.get('HITPAY_PAYMENT_METHODS', '')

_AI_PHOTOS_YOLO_MODEL_PATH = os.environ.get('AI_PHOTOS_YOLO_MODEL_PATH')
AI_PHOTOS_YOLO_MODEL_PATH = str(
    Path(_AI_PHOTOS_YOLO_MODEL_PATH)
    if _AI_PHOTOS_YOLO_MODEL_PATH and Path(_AI_PHOTOS_YOLO_MODEL_PATH).is_absolute()
    else BASE_DIR / (_AI_PHOTOS_YOLO_MODEL_PATH or 'models/best.pt')
)
AI_PHOTOS_YOLO_CONF = float(os.environ.get('AI_PHOTOS_YOLO_CONF', '0.05'))
AI_PHOTOS_YOLO_IOU = float(os.environ.get('AI_PHOTOS_YOLO_IOU', '0.50'))
AI_PHOTOS_YOLO_MAX_DET = int(os.environ.get('AI_PHOTOS_YOLO_MAX_DET', '50'))
AI_PHOTOS_YOLO_IMGSZ = int(os.environ.get('AI_PHOTOS_YOLO_IMGSZ', '1280'))
AI_PHOTOS_OCR_CONF = float(os.environ.get('AI_PHOTOS_OCR_CONF', '0.50'))
AI_PHOTOS_UPSCALE_FACTOR = int(os.environ.get('AI_PHOTOS_UPSCALE_FACTOR', '3'))
AI_PHOTOS_CROP_PADDING_RATIO = float(os.environ.get('AI_PHOTOS_CROP_PADDING_RATIO', '0.04'))
AI_PHOTOS_AUTO_CONFIDENCE = float(os.environ.get('AI_PHOTOS_AUTO_CONFIDENCE', '0.85'))
AI_PHOTOS_REVIEW_CONFIDENCE = float(os.environ.get('AI_PHOTOS_REVIEW_CONFIDENCE', '0.50'))

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
