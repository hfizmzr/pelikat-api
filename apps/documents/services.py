"""
Document Encryption Service

AES-256-GCM encrypt/decrypt for IC/passport images.
In-memory OCR validation runs before any bytes touch storage.
If validation fails, the file is rejected without a storage footprint.

Format v2 (Migration note: accepted as irreversible by the audit review):
v1 blobs were uploaded with iv/tag held only in the HTTP response, never
persisted — every v1 blob is unrecoverable. v2 is self-describing:
    {"ver":2,"iv":<b64>,"ct":<b64>,"tag":<b64>}   (JSON, uploaded as-is)
Keys are HKDF-derived per user, and the AEAD associates the user id as
AAD, so ciphertext cannot be transplanted across profiles.
"""

import base64
import io
import json
import logging
import os
import posixpath
import time
from datetime import UTC, datetime

import cv2
import numpy as np

# ── Paddle FLAGS must be set before any Paddle/PaddleOCR import ──
# Disable MKLDNN, OneDNN, and PIR API to force the legacy CPU inference
# path that works with PaddlePaddle 3.0.0 on CPU (PP-OCRv5_mobile models).
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_use_onednn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_pir_api", "0")
os.environ.setdefault("FLAGS_allocator_strategy", "naive_best_fit")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "1")

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from PIL import Image  # module unwarped: header peek only, no full decode
from supabase import create_client

from .mrz import parse_td3, passport_number_valid
from .nric import extract_nric_from_text, validate_nric

logger = logging.getLogger(__name__)

BUCKET = "ic-documents"

MAX_UPLOAD_BYTES = 10 * 1024 * 1024      # mirrors frontend "Max 10MB"
MAX_IMAGE_DIMENSION = 8000               # px per side
MAX_IMAGE_PIXELS = 24_000_000            # ≤ 24MP

_MAGIC_HEADS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

_KEYWORDS = [
    r"KAD PENGENALAN", r"IDENTITY CARD", r"WARGA NEGARA",
    r"PASSPORT", r"MALAYSIA", r"JABATAN PENDAFTARAN", r"IMMIGRATION",
]

_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="en_PP-OCRv5_mobile_rec",
        )
    return _ocr


def get_supabase_client():
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY
    )


# ── v2 envelope: per-user HKDF key + user-bound AAD ──────────────────


def _derive_key(user_id: str, purpose: str) -> bytes:
    """HKDF per user. `purpose` separates document blobs from IC-string
    values so the two ciphertext families cannot be mixed."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"pelikat-ic-{purpose}-v2:{user_id}".encode(),
    )
    return hkdf.derive(bytes.fromhex(settings.DOCUMENT_ENCRYPTION_KEY))


def _user_aad(user_id: str, purpose: str) -> bytes:
    return f"pelikat-ic-{purpose}-v2:{user_id}".encode()


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """AES-256-GCM → self-describing v2 envelope (JSON bytes)."""
    nonce = os.urandom(12)
    ct_with_tag = AESGCM(key).encrypt(nonce, plaintext, aad)
    return json.dumps({
        "ver": 2,
        "iv": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ct_with_tag[:-16]).decode(),
        "tag": base64.b64encode(ct_with_tag[-16:]).decode(),
    }, separators=(",", ":")).encode()


def _unseal(envelope: bytes | str, key: bytes, aad: bytes) -> bytes:
    """Verify + decrypt a v2 envelope. Legacy v1 (no persisted iv/tag)
    is documented-unrecoverable and rejected explicitly."""
    try:
        env = json.loads(envelope)
    except (ValueError, TypeError):
        raise ValueError("invalid envelope")
    if not isinstance(env, dict) or env.get("ver") != 2:
        # v1 ciphertext was stored without iv/tag — plaintext is lost
        # by design (audit decision). Callers surface this verbatim.
        raise ValueError("legacy_unrecoverable")
    try:
        ciphertext = base64.b64decode(env["ct"]) + base64.b64decode(env["tag"])
        return AESGCM(key).decrypt(base64.b64decode(env["iv"]), ciphertext, aad)
    except (ValueError, KeyError, InvalidTag):
        raise ValueError("decryption failed: wrong key or tampered ciphertext")


def _encrypt_ic_number(ic_number: str, user_id: str) -> str:
    """Encrypt an IC/passport string: per-user HKDF key + user-bound AAD
    (same scheme as document blobs), returned as the v2 envelope JSON."""
    key = _derive_key(user_id, "ic_number")
    return _seal(key, ic_number.encode("utf-8"), _user_aad(user_id, "ic_number"))\
        .decode()


# ── upload guardrails (pure helpers, no OCR) ─────────────────────────


def sniff_image_mime(data: bytes) -> str | None:
    """Magic-byte sniff restricted to the accepted photo formats."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def assert_image_dimensions(data: bytes) -> None:
    """PIL header peek (no full decode): cap per-side ≤ 8000 and total
    pixels ≤ 24MP. Raises ValueError to be mapped to HTTP 413."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception as exc:
        logger.warning("image header peek failed: %s", exc)
        return  # let the cv2 imdecode step produce the 422 message
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValueError(
            "Image is too large. Please upload a photo of at most 8000×8000 pixels.")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError(
            "Image is too large. Please upload a photo of at most 24 megapixels.")


# ── OCR pipeline ─────────────────────────────────────────────────────


def _iter_ocr_lines(ocr_result):
    """Parse PaddleOCR 3.x results (handles both dict and legacy list formats)."""
    if not ocr_result:
        return
    for page in ocr_result:
        if isinstance(page, dict):
            texts = page.get("rec_texts") or []
            scores = page.get("rec_scores") or []
            for text, score in zip(texts, scores):
                yield str(text), float(score)
            continue
        for line in page or []:
            if len(line) < 2 or len(line[1]) < 2:
                continue
            yield str(line[1][0]), float(line[1][1])


def validate_document(file_bytes: bytes, *, dob: str | None = None,
                      gender: str | None = None) -> dict:
    """
    In-memory OCR validation. Returns::

      {"is_valid": False, "reason": "..."}            — outright rejection
      {"is_valid": True, "needs_review": False}       — fully valid
      {"is_valid": True, "needs_review": True,
       "warnings": [...]}                             — accepted, flags attached

    Result extras: "nric" (canonical NRIC or None) is filled when a
    valid NRIC is detected; "passport_number" when a well-formed MRZ
    is present (MRZ is authoritative for passports and its check digits
    MUST pass, otherwise the upload is a hard reject).
    """
    ocr = _get_ocr()
    nparr = np.frombuffer(file_bytes, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if image is None:
        return {
            "is_valid": False,
            "needs_review": False,
            "reason": "Could not decode the uploaded image. Please upload a valid photo.",
        }
    results = ocr.ocr(image)

    texts: list[str] = []
    confidences: list[float] = []
    for text, confidence in _iter_ocr_lines(results):
        texts.append(text)
        confidences.append(confidence)

    if not texts:
        return {
            "is_valid": False,
            "needs_review": False,
            "reason": "No text detected. Please upload a clear photo of your IC or Passport.",
        }

    all_text = " ".join(texts).strip()

    if len(all_text) < 10:
        return {
            "is_valid": False,
            "needs_review": False,
            "reason": "Not enough text found. The image may be too blurry or cropped. Please try again.",
        }

    avg_confidence = sum(confidences) / len(confidences) if confidences else 1.0

    # MRZ authoritative path (High #5): any parseable TD3 for a passport
    # number becomes the trusted value and its check digits must pass.
    mrz = parse_td3("\n".join(texts))
    if mrz is not None:
        if mrz["errors"] or not passport_number_valid(mrz["passport_number"]):
            return {
                "is_valid": False,
                "needs_review": False,
                "reason": (
                    "The passport MRZ check digits failed validation. The image may "
                    "be fabricated or the scan is unreadable — please re-upload."),
            }
        result = {
            "is_valid": True,
            "needs_review": avg_confidence < 0.60,
            "warnings": [],
            "nric": None,
            "passport_number": mrz["passport_number"],
        }
        if result["needs_review"]:
            result["reason"] = ("Accepted, but image quality is low. Your document "
                                "has been saved but may require manual review.")
        return result

    text_upper = all_text.upper()
    keyword_match = any(kw in text_upper for kw in _KEYWORDS)

    nric_value = extract_nric_from_text(text_upper)
    needs_review = False
    warnings: list[str] = []

    if nric_value:
        outcome = validate_nric(nric_value, dob=dob, gender=gender, today=None)
        if not outcome["valid"]:
            # OCR misreads are common — soft-fail rather than reject.
            warnings.append(f"Detected NRIC {nric_value} could not be fully validated: {outcome['reason']}")
            needs_review = True
            nric_value = None
        else:
            warnings.extend(outcome["warnings"])
            needs_review = needs_review or bool(outcome["warnings"])

    if not keyword_match and not nric_value:
        return {
            "is_valid": False,
            "needs_review": False,
            "reason": "No identity document text found. This does not appear to be an IC or Passport. Please upload the correct document.",
        }

    if not nric_value:
        # Keyword present but no number extracted → reviewer must key it in.
        warnings.append("OCR could not read the IC/passport number from this image.")
        needs_review = True

    if avg_confidence < 0.60:
        warnings.append(
            "Accepted, but image quality is low. Your document has been saved but may require manual review.")
        needs_review = True

    return {"is_valid": True, "needs_review": needs_review,
            "warnings": warnings, "nric": nric_value}


def encrypt_document(file_bytes: bytes, user_id: str, nric: str | None = None) -> dict:
    """
    Encrypt raw document bytes (AES-256-GCM, v2 envelope) and upload the
    blob. Optionally encrypts an OCR-extracted NRIC for the profile.

    Returns:
        { document_path, uploaded_at, [nric_encrypted] }
    """
    sb = get_supabase_client()

    key = _derive_key(user_id, "document")
    envelope = _seal(key, file_bytes, _user_aad(user_id, "document"))

    ts = int(time.time() * 1000)
    encrypted_path = f"{user_id}/{ts}.enc"

    sb.storage.from_(BUCKET).upload(
        path=encrypted_path,
        file=envelope,
        file_options={"content-type": "application/json", "upsert": "true"},
    )

    result: dict = {
        "document_path": encrypted_path,
        "uploaded_at": datetime.fromtimestamp(ts / 1000, tz=UTC).isoformat(),
    }

    if nric:
        result["nric_encrypted"] = _encrypt_ic_number(nric, user_id)

    return result


def decrypt_document(encrypted_path: str, user_id: str) -> bytes:
    """
    Internal/admin path only — deliberately NOT exposed via any URL.
    Fetch + verify a v2 enclosure in memory. Legacy v1 blobs (no persisted
    iv/tag) fail with "legacy_unrecoverable" by design.
    """
    sb = get_supabase_client()
    envelope = sb.storage.from_(BUCKET).download(encrypted_path)
    return _unseal(envelope, _derive_key(user_id, "document"),
                   _user_aad(user_id, "document"))


def is_safe_document_path(path: str, user_id: str) -> bool:
    """Storage keys are '{user_id}/{timestamp_ms}.enc'. Reject anything
    escaping that prefix (IDOR / path traversal)."""
    if not isinstance(path, str) or "\\" in path or ".." in path.split("/"):
        return False
    return path == posixpath.normpath(path) and path.startswith(f"{user_id}/")


def delete_document(encrypted_path: str) -> None:
    """Delete encrypted IC document from storage."""
    sb = get_supabase_client()
    sb.storage.from_(BUCKET).remove([encrypted_path])
