import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-change-this-in-production')

DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'

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
}

HMAC_SECRET = os.environ.get('HMAC_SECRET', '')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
SUPABASE_JWT_SECRET = os.environ.get('SUPABASE_JWT_SECRET', '')
INTERNAL_API_KEY = os.environ.get('INTERNAL_API_KEY', '')
DOCUMENT_ENCRYPTION_KEY = os.environ.get('DOCUMENT_ENCRYPTION_KEY', '')

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
