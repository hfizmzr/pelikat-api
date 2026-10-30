"""
Documents service layer: v2 crypto envelope, AAD binding, upload guardrails.
OCR + Supabase storage + Paddle are mocked; nothing touches the network.
"""
import json
from unittest.mock import MagicMock

import pytest

from apps.documents import services

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def mock_supabase(monkeypatch):
    sb = MagicMock()
    store: dict[str, bytes] = {}
    bucket = sb.storage.from_.return_value
    bucket.upload.side_effect = lambda path, file, file_options=None: store.setdefault(path, file)
    bucket.download.side_effect = lambda path: store[path]
    bucket.remove.side_effect = lambda paths: [store.pop(p, None) for p in paths]
    monkeypatch.setattr(services, "get_supabase_client", lambda: sb)
    return sb, store


@pytest.mark.security
class TestV2Envelope:
    def test_encrypt_document_uploads_self_describing_blob(self, mock_supabase):
        sb, _store = mock_supabase
        result = services.encrypt_document(b"raw-bytes", USER)
        kwargs = sb.storage.from_().upload.call_args.kwargs
        envelope = json.loads(kwargs["file"].decode())
        assert envelope["ver"] == 2
        assert set(envelope) == {"ver", "iv", "ct", "tag"}
        assert kwargs["file_options"]["content-type"] == "application/octet-stream"
        assert kwargs["path"] == result["document_path"]
        assert result["document_path"].startswith(USER + "/")
        assert "T" in result["uploaded_at"]  # iso8601

    def test_roundtrip_internal_decrypt(self, mock_supabase):
        result = services.encrypt_document(b"raw-bytes", USER)
        assert services.decrypt_document(result["document_path"], USER) == b"raw-bytes"

    def test_ciphertext_not_transplantable_across_users(self):
        # v2 AAD binding: user A's blob cannot be decrypted as user B
        sealed = services._seal(services._derive_key(USER, "document"), b"raw",
                                services._user_aad(USER, "document"))
        with pytest.raises(ValueError):
            services._unseal(sealed, services._derive_key(OTHER, "document"),
                             services._user_aad(OTHER, "document"))

    def test_document_and_ic_purposes_separated(self):
        ic_b64 = services._encrypt_ic_number("900101-14-5123", USER)
        with pytest.raises(ValueError):
            services._unseal(ic_b64, services._derive_key(USER, "document"),
                             services._user_aad(USER, "document"))

    def test_master_key_never_used_raw(self):
        # _encrypt_ic_number output decrypts with the HKDF key only
        ic = services._encrypt_ic_number("X", USER)
        assert services._unseal(ic, services._derive_key(USER, "ic_number"),
                                services._user_aad(USER, "ic_number")) == b"X"

    def test_legacy_envelope_unrecoverable(self):
        v1 = json.dumps({"ver": 1, "iv": "AA==", "ct": "AA==", "tag": "AA=="})
        with pytest.raises(ValueError, match="legacy_unrecoverable"):
            services._unseal(v1, services._derive_key(USER, "document"),
                             services._user_aad(USER, "document"))


@pytest.mark.security
class TestUploadGuardrails:
    def test_jpeg_png_webp_sniffed(self):
        assert services.sniff_image_mime(b"\xff\xd8\xff" + b"\x00" * 40) == "image/jpeg"
        assert services.sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40) == "image/png"
        assert services.sniff_image_mime(b"RIFF\x00\x01\x02\x03WEBP" + b"\x00" * 40) == "image/webp"
        assert services.sniff_image_mime(b"GIF89a" + b"\x00" * 40) is None
        assert services.sniff_image_mime(b"") is None

    def test_dimension_caps(self):
        import io

        from PIL import Image

        def png_with_dims(w, h):
            # minimal valid PNG header via PIL-generated real file
            img = Image.new("RGB", (w, h), color=(0, 0, 0))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()

        services.assert_image_dimensions(png_with_dims(4000, 3000))  # ok
        with pytest.raises(ValueError):
            services.assert_image_dimensions(png_with_dims(8001, 10))
        with pytest.raises(ValueError):
            services.assert_image_dimensions(png_with_dims(10, 8001))
        with pytest.raises(ValueError):
            services.assert_image_dimensions(png_with_dims(6000, 4001))  # > 24MP
        services.assert_image_dimensions(png_with_dims(8000, 3000))  # boundary ok

    def test_max_upload_size_constant(self):
        assert services.MAX_UPLOAD_BYTES == 10 * 1024 * 1024

    def test_decompression_bomb_rejected_before_cv2(self):
        """PIL Image.open raises DecompressionBombError for images above the
        Pillow pixel limit. assert_image_dimensions must re-raise as its own
        contract (ValueError → HTTP 413) and NEVER silently return: a silent
        return sends the same bytes into cv2.imdecode, which allocates the
        decompressed buffer before any pixel-cap check can reject it.

        Uses a 66-byte synthetic PNG whose IHDR declares 20000×20000 (400MP,
        far above any cap) with an undecodable IDAT — no 1.2GB allocation. """
        import struct
        import zlib

        def fake_bomb(w, h):
            sig = b"\x89PNG\r\n\x1a\n"
            ihdr_data = struct.pack(">II5B", w, h, 8, 2, 0, 0, 0)
            ihdr = struct.pack(
                ">I", 13) + b"IHDR" + ihdr_data + struct.pack(
                ">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xffffffff)
            z = zlib.compress(b"\x00")
            idat_data = b"IDAT" + z
            idat = struct.pack(">I", len(z)) + idat_data + struct.pack(
                ">I", zlib.crc32(idat_data) & 0xffffffff)
            iend_data = b"IEND"
            iend = struct.pack(">I", 0) + iend_data + struct.pack(
                ">I", zlib.crc32(iend_data) & 0xffffffff)
            return sig + ihdr + idat + iend

        with pytest.raises(ValueError):
            services.assert_image_dimensions(fake_bomb(20000, 20000))

    def test_truncated_image_maps_to_value_error(self):
        """Valid JPEG magic bytes + garbage payload: sniff passes, but Pillow
        fails to decode (UnidentifiedImageError/OSError). Must surface as the
        contract ValueError (→ HTTP 4xx), never as an unhandled 500."""
        jpeg_garbage = b"\xff\xd8\xff\xe0" + b"not-really-a-jpeg" * 10
        with pytest.raises(ValueError):
            services.assert_image_dimensions(jpeg_garbage)


@pytest.mark.security
class TestPathSafety:
    @pytest.mark.parametrize("path", [
        f"{USER}/1700000000000.enc",
        f"{USER}/{123}.enc",
    ])
    def test_safe(self, path):
        assert services.is_safe_document_path(path, USER)

    @pytest.mark.parametrize("path", [
        f"{OTHER}/1700000000000.enc",          # IDOR: other user's blob
        f"{USER}_other/1700000000000.enc",     # prefix smuggling
        "",                                       # empty
        f"{USER}/../../PUBLIC/secret",          # traversal
        f"{USER}/1/../x.enc",
        f"{USER}\\1.enc",                        # backslash
        f"{USER}/a//b.enc",                      # not normalized
    ])
    def test_unsafe(self, path):
        assert services.is_safe_document_path(path, USER) is False
