"""
Document Encryption Service

AES-256-GCM encrypt/decrypt for IC/passport images.
In-memory OCR validation runs before any bytes touch storage.
If validation fails, the file is rejected without a storage footprint.
"""

import os
import base64
import re
import time

import numpy as np
import cv2

# ── Paddle FLAGS must be set before any Paddle/PaddleOCR import ──
# Disable MKLDNN, OneDNN, and PIR API to force the legacy CPU inference
# path that works with PaddlePaddle 3.0.0 on CPU (PP-OCRv5_mobile models).
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_use_onednn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_pir_api", "0")
os.environ.setdefault("FLAGS_allocator_strategy", "naive_best_fit")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "1")

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from supabase import create_client
from django.conf import settings


BUCKET = "ic-documents"

_KEYWORDS = [
    r"KAD PENGENALAN", r"IDENTITY CARD", r"WARGA NEGARA",
    r"PASSPORT", r"MALAYSIA", r"JABATAN PENDAFTARAN", r"IMMIGRATION",
]
_NRIC_PATTERN = re.compile(r"\d{6}-\d{2}-\d{4}")

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


def _derive_key(user_id: str) -> bytes:
    master = bytes.fromhex(settings.DOCUMENT_ENCRYPTION_KEY)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"pelikat-ic-document-v1:" + user_id.encode(),
    )
    return hkdf.derive(master)


def _encrypt_ic_number(ic_number: str) -> str:
    """Encrypt an IC number with AES-256-GCM using the document key."""
    master = bytes.fromhex(settings.DOCUMENT_ENCRYPTION_KEY)
    nonce = os.urandom(12)
    aesgcm = AESGCM(master)
    ciphertext = aesgcm.encrypt(nonce, ic_number.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


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


def validate_document(file_bytes: bytes) -> dict:
    """
    In-memory OCR validation.  Returns::

      {"is_valid": True}                             — fully valid
      {"is_valid": True, "needs_review": True,
       "reason": "..."}                              — match found but low confidence
      {"is_valid": False, "needs_review": False,
       "reason": "..."}                              — outright rejection
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

    text_upper = all_text.upper()
    keyword_match = any(kw in text_upper for kw in _KEYWORDS)
    nric = _NRIC_PATTERN.search(text_upper)
    nric_match = bool(nric)

    if not keyword_match and not nric_match:
        return {
            "is_valid": False,
            "needs_review": False,
            "reason": "No identity document text found. This does not appear to be an IC or Passport. Please upload the correct document.",
        }

    avg_confidence = sum(confidences) / len(confidences) if confidences else 1.0
    ic_number = nric.group(0) if nric else None

    if avg_confidence < 0.60:
        return {
            "is_valid": True,
            "needs_review": True,
            "reason": "Accepted, but image quality is low. Your document has been saved but may require manual review.",
            "ic_number": ic_number,
        }

    return {"is_valid": True, "needs_review": False, "ic_number": ic_number}


def encrypt_document(file_bytes: bytes, user_id: str, ic_number: str | None = None) -> dict:
    """
    Encrypt raw document bytes with AES-256-GCM and upload the encrypted blob.
    Optionally encrypts an IC number extracted by OCR.

    Returns:
        { encrypted_path, iv, tag, [ic_encrypted] } — iv and tag are base64-encoded
    """
    sb = get_supabase_client()

    derived_key = _derive_key(user_id)
    aesgcm = AESGCM(derived_key)
    nonce = os.urandom(12)

    ciphertext = aesgcm.encrypt(nonce, file_bytes, None)
    tag = ciphertext[-16:]
    body = ciphertext[:-16]

    ts = str(int(time.time() * 1000))
    encrypted_path = f"{user_id}/{ts}.enc"

    sb.storage.from_(BUCKET).upload(
        path=encrypted_path,
        file=body,
        file_options={"content-type": "application/octet-stream", "upsert": "true"},
    )

    result: dict = {
        "encrypted_path": encrypted_path,
        "iv": base64.b64encode(nonce).decode("utf-8"),
        "tag": base64.b64encode(tag).decode("utf-8"),
    }

    if ic_number:
        result["ic_encrypted"] = _encrypt_ic_number(ic_number)

    return result


def decrypt_document(encrypted_path: str, iv_b64: str, tag_b64: str, user_id: str) -> bytes:
    """
    Fetch encrypted blob, decrypt in memory, return raw bytes.
    Bytes are never persisted — caller is responsible for transient use.
    """
    sb = get_supabase_client()

    encrypted_bytes = sb.storage.from_(BUCKET).download(encrypted_path)

    nonce = base64.b64decode(iv_b64)
    tag = base64.b64decode(tag_b64)
    ciphertext_with_tag = encrypted_bytes + tag

    derived_key = _derive_key(user_id)
    aesgcm = AESGCM(derived_key)

    plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag, None)
    return plaintext


def delete_document(encrypted_path: str) -> None:
    """Delete encrypted IC document from storage."""
    sb = get_supabase_client()
    sb.storage.from_(BUCKET).remove([encrypted_path])
