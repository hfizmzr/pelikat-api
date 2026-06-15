"""
Document Views
"""

import base64
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .services import encrypt_document, decrypt_document, delete_document, validate_document


@api_view(["POST"])
def encrypt_view(request):
    """
    POST /ai/documents/encrypt

    Request (multipart/form-data):
      document:  image file (JPEG/PNG/WebP)
      user_id:   uuid string

    Flow:
      1. Validate document in memory via OCR (no bytes touch storage)
      2. If validation fails → 422 with structured rejection reason
      3. If low-confidence match → 201 with warning flag
      4. Encrypt with AES-256-GCM → upload encrypted blob → delete raw temp
      5. Return { encrypted_path, iv, tag, [warning] }
    """
    uploaded = request.FILES.get("document")
    user_id = request.data.get("user_id")

    if not uploaded or not user_id:
        return Response({"error": "document and user_id are required"}, status=400)

    file_bytes = uploaded.read()
    uploaded.seek(0)

    validation = validate_document(file_bytes)

    if not validation["is_valid"]:
        return Response({"error": validation["reason"]}, status=422)

    try:
        result = encrypt_document(file_bytes, user_id, validation.get("ic_number"))
    except Exception as e:
        return Response({"error": str(e)}, status=500)

    if validation.get("needs_review"):
        return Response({**result, "warning": validation["reason"]}, status=201)

    return Response(result, status=201)


@api_view(["POST"])
def decrypt_view(request):
    """
    POST /ai/documents/decrypt

    Request:
      { "encrypted_path": "{user_id}/{ts}.enc",
        "iv": "<base64>",
        "tag": "<base64>",
        "user_id": "uuid" }

    Response:
      { "image": "<base64-encoded decrypted image>",
        "mime": "image/jpeg" }

    Security:
      - Decryption happens in memory only
      - The image is returned as a base64 string, never persisted to disk
      - Only admin JWT roles should call this; caller must enforce RBAC
    """
    encrypted_path = request.data.get("encrypted_path")
    iv_b64 = request.data.get("iv")
    tag_b64 = request.data.get("tag")
    user_id = request.data.get("user_id")

    if not all([encrypted_path, iv_b64, tag_b64, user_id]):
        return Response(
            {"error": "encrypted_path, iv, tag, and user_id are required"},
            status=400,
        )

    try:
        plaintext = decrypt_document(encrypted_path, iv_b64, tag_b64, user_id)
        return Response({
            "image": base64.b64encode(plaintext).decode("utf-8"),
            "mime": "image/jpeg",
        })
    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(["POST"])
def delete_view(request):
    """
    POST /ai/documents/delete

    Request:
      { "encrypted_path": "{user_id}/{ts}.enc" }

    Used during account deletion to remove the encrypted IC document
    before anonymizing the profile.
    """
    encrypted_path = request.data.get("encrypted_path")

    if not encrypted_path:
        return Response({"error": "encrypted_path is required"}, status=400)

    try:
        delete_document(encrypted_path)
        return Response({"deleted": True})
    except Exception as e:
        return Response({"error": str(e)}, status=500)
