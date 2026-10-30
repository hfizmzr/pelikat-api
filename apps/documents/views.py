"""
Document Views

All responses keep the frontend (BFF) contract:
201 → {"status": "ok", ...}
"""

import logging
import re
import uuid

from rest_framework.decorators import api_view, parser_classes, throttle_classes
from rest_framework.response import Response

from .nric import NRIC_RE, validate_nric
from .parsers import BoundedMultiPartParser
from .services import (
    MAX_UPLOAD_BYTES,
    _encrypt_ic_number,
    assert_image_dimensions,
    delete_document,
    encrypt_document,
    is_safe_document_path,
    sniff_image_mime,
    validate_document,
)
from .throttling import UserScopedRateThrottle

logger = logging.getLogger(__name__)

# Anonymous throttle intentionally excluded: every request here is anonymous
# (internal-key middleware, no auth classes), so a shared "anon" bucket would
# rate-limit ALL BFF traffic collectively during registration peak. The
# UserScopedRateThrottle already keys per user id / forwarded client IP.
_THROTTLES = [UserScopedRateThrottle]
_PASSPORT_RE = r"^[A-Za-z0-9]{5,15}$"


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return False
    return True


def _ensure_dob_gender_valid(data) -> Response | None:
    """400 when the optional dob/gender fields are non-strings."""
    dob, gender = data.get("dob"), data.get("gender")
    if dob and not isinstance(dob, str):
        return Response({"error": "dob must be a string"}, status=400)
    if gender and not isinstance(gender, str):
        return Response({"error": "gender must be a string"}, status=400)
    return None


@api_view(["POST"])
@parser_classes([BoundedMultiPartParser])
@throttle_classes(_THROTTLES)
def encrypt_view(request):
    """
    POST /ai/documents/encrypt (multipart/form-data)

      document:  image file (JPEG/PNG/WebP, max 10MB)
      user_id:   uuid string — storage path is built from this, never
                 taken from any client path field
      [dob]:     optional ISO YYYY-MM-DD (BFF-passed profile DOB)
      [gender]:  optional "male"/"female" for NRIC cross-check

    201 → {"status": "ok",
           "document_path": "{user_id}/{ts}.enc",
           "document_mime": sniffed mime,
           "uploaded_at": iso8601,
           "nric_encrypted": v2 envelope JSON string | null,
           "needs_review": bool,
           "warnings": [...]}
    """
    uploaded = request.FILES.get("document")
    user_id = request.data.get("user_id")

    if not uploaded or not user_id:
        return Response({"error": "document and user_id are required"}, status=400)

    if not _is_uuid(user_id):
        return Response({"error": "user_id must be a UUID"}, status=400)

    # Stream-read with a hard byte cap BEFORE assembling the full buffer.
    # (also rejects File objects whose declared size lies — no memory bomb)
    file_parts: list[bytes] = []
    total = 0
    while True:
        chunk = uploaded.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            return Response(
                {"error": "Document exceeds the 10MB limit. Please upload a smaller photo."},
                status=413)
        file_parts.append(chunk)
    file_bytes = b"".join(file_parts)

    mime = sniff_image_mime(file_bytes)
    if not mime:
        return Response(
            {"error": "Unsupported file type. Please upload a JPEG, PNG or WebP photo."},
            status=415)

    try:
        assert_image_dimensions(file_bytes)
    except ValueError:
        # Details stay in the log only — echoing exception text to the
        # client would leak stack/internal info (CodeQL py/stack-trace-exposure).
        logger.info("image validation rejected (user=%s)", user_id)
        return Response(
            {"error": "Image is too large or unreadable. Please upload a smaller, valid photo."},
            status=413)

    dob = request.data.get("dob") or None
    gender = request.data.get("gender") or None
    early = _ensure_dob_gender_valid(request.data)
    if early:
        return early

    validation = validate_document(file_bytes, dob=dob, gender=gender)

    if not validation["is_valid"]:
        return Response({"error": validation["reason"]}, status=422)

    try:
        # Identity number for the envelope: NRIC when detected, otherwise the
        # MRZ passport number. Without the fallback, a fully valid passport
        # upload stores no encrypted id at all (nric_encrypted null) and,
        # needs_review being False in the clean-MRZ path, no reviewer is
        # ever asked to key it in — data lost silently.
        id_number = validation.get("nric") or validation.get("passport_number")
        result = encrypt_document(file_bytes, user_id, id_number)
    except Exception:
        logger.exception("encrypt_document failed for user %s", user_id)
        return Response(
            {"error": "Could not store the encrypted document. Please try again later."},
            status=500)

    warnings = list(validation.get("warnings") or [])
    return Response({
        "status": "ok",
        "document_path": result["document_path"],
        "document_mime": mime,
        "uploaded_at": result["uploaded_at"],
        "nric_encrypted": result.get("nric_encrypted"),
        "needs_review": bool(validation.get("needs_review")) or bool(warnings),
        "warnings": warnings,
    }, status=201)


@api_view(["POST"])
@throttle_classes(_THROTTLES)
def encrypt_ic_view(request):
    """
    POST /ai/documents/encrypt-ic (JSON)

      {"ic_number": "900101-14-5123", "user_id": uuid,
       ["dob": iso-date, "gender": male|female]}

    Manual-entry fallback for runners without an IC photo. Valid NRICs
    get the full canonical rule-set; explicit DOB mismatches hard-reject.
    Non-NRIC values are passports: loose alnum format only (an OCR MRZ
    cannot be verified on manual entry → needs_review).

    201 → {"status": "ok", "ic_encrypted": v2 envelope JSON string,
           "needs_review": bool, "warnings": [...]}
    """
    # Non-mapping JSON bodies (array/null) must fall through to the
    # missing-field 400, not AttributeError → 500 (same guard as throttling).
    data = request.data if isinstance(request.data, dict) else {}
    ic_number = data.get("ic_number")
    user_id = data.get("user_id")
    dob = data.get("dob") or None
    gender = data.get("gender") or None
    early = _ensure_dob_gender_valid(data)
    if early:
        return early

    if not ic_number or not user_id:
        return Response({"error": "ic_number and user_id are required"}, status=400)
    if not _is_uuid(user_id):
        return Response({"error": "user_id must be a UUID"}, status=400)
    if not isinstance(ic_number, str):
        return Response({"error": "ic_number must be a string"}, status=422)
    ic_number = ic_number.strip()

    warnings: list[str] = []
    needs_review = False

    if NRIC_RE.fullmatch(ic_number):
        # NRIC shape → full canonical rule-set.
        outcome = validate_nric(ic_number, dob=dob, gender=gender)
        if not outcome["valid"]:
            return Response({"error": outcome["reason"]}, status=422)
        warnings.extend(outcome["warnings"])
        needs_review = bool(warnings)
    elif not re.fullmatch(_PASSPORT_RE, ic_number):
        return Response({"error": "Invalid IC/Passport number format"}, status=422)
    else:
        # Loose-format passport entry has no MRZ verification → review.
        warnings.append("Passport number accepted on format only — no MRZ present to verify.")
        needs_review = True

    return Response({
        "status": "ok",
        "ic_encrypted": _encrypt_ic_number(ic_number, user_id),
        "needs_review": needs_review,
        "warnings": warnings,
    }, status=201)


@api_view(["POST"])
@throttle_classes(_THROTTLES)
def delete_view(request):
    """
    POST /ai/documents/delete (JSON)
      {"user_id": uuid, "encrypted_path": "{user_id}/{ts}.enc"}

    Used during account deletion to remove the encrypted IC document
    before anonymizing the profile. The path MUST live under the
    caller's user_id prefix (IDOR/traversal otherwise 403).
    """
    data = request.data if isinstance(request.data, dict) else {}
    user_id = data.get("user_id")
    encrypted_path = data.get("encrypted_path")

    if not user_id or not encrypted_path:
        return Response({"error": "user_id and encrypted_path are required"}, status=400)

    if not _is_uuid(user_id):
        return Response({"error": "user_id must be a UUID"}, status=400)

    if not is_safe_document_path(encrypted_path, user_id):
        return Response({"error": "Forbidden"}, status=403)

    try:
        delete_document(encrypted_path)
        return Response({"deleted": True})
    except Exception:
        logger.exception("delete_document failed for user %s path %s", user_id, encrypted_path)
        return Response(
            {"error": "Could not delete the document. Please try again later."},
            status=500)
