"""
Documents HTTP API contract + security (Critical #3/#4/#7, Low #15).

OCR is monkeypatched (no Paddle), Supabase storage mocked. Headless test:
    pytest apps/tests/test_documents_api.py
"""
import json
import sys
import types
from io import BytesIO
from unittest.mock import MagicMock

# Stub heavy optional deps absent from the test venv (ai_photos urlconf
# import chain needs them at module level only).
if "ultralytics" not in sys.modules:
    _fake = types.ModuleType("ultralytics")
    _fake.YOLO = lambda *a, **k: MagicMock()
    sys.modules["ultralytics"] = _fake

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from apps.documents import services, views

USER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _h(headers=()):
    h = {"HTTP_X_INTERNAL_KEY": "test-internal-api-key-12345"}
    h.update(headers)
    return h


def _make_png(w=100, h=100):
    buf = BytesIO()
    Image.new("RGB", (w, h), color=(7, 7, 7)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def store(monkeypatch):
    sb = MagicMock()
    blob_store: dict[str, bytes] = {}
    bucket = sb.storage.from_.return_value
    bucket.upload.side_effect = lambda path, file, file_options=None: blob_store.setdefault(path, file)
    bucket.download.side_effect = lambda path: blob_store[path]
    bucket.remove.side_effect = lambda paths: [blob_store.pop(p, None) for p in paths]
    monkeypatch.setattr(services, "get_supabase_client", lambda: sb)
    return blob_store


@pytest.fixture
def ocr(monkeypatch):
    """Fake OCR: static high-confidence IC/text page, no Paddle import."""
    fake = MagicMock()
    fake.ocr.return_value = [{
        "rec_texts": ["KAD PENGENALAN", "TAN MEI LING 010101-14-5123", "MALAYSIA"],
        "rec_scores": [0.95, 0.95, 0.95],
    }]
    monkeypatch.setattr(services, "_get_ocr", lambda: fake)
    yield fake


@pytest.mark.security
class TestEncryptEndpoint:
    def test_missing_params_400(self, store, ocr):
        r = Client().post("/ai/documents/encrypt", **_h())
        assert r.status_code == 400
        assert r.json()["error"] == "document and user_id are required"

    def test_no_key_401(self, store, ocr):
        r = Client().post("/ai/documents/encrypt", data={"user_id": USER})
        assert r.status_code == 401

    def test_non_uuid_user_id_rejected(self, store, ocr):
        r = Client().post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("ic.png", _make_png()), "user_id": "not-a-uuid"},
            **_h())
        assert r.status_code == 400

    def test_oversize_413_before_ocr(self, store, ocr, monkeypatch):
        seen = []
        monkeypatch.setattr(services, "_get_ocr", lambda: (seen.append(1), MagicMock())[1])
        monkeypatch.setattr(views, "MAX_UPLOAD_BYTES", 100)
        r = Client().post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("ic.png", _make_png() * 20),  # ~1.3KB
                  "user_id": USER},
            **_h())
        assert r.status_code == 413
        assert not seen

    def test_bad_mime_415(self, store, ocr):
        r = Client().post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("sniff.gif", b"GIF89a" + b"\x00" * 64),
                  "user_id": USER},
            **_h())
        assert r.status_code == 415

    def test_contract_201_full_path(self, store, ocr):
        r = Client().post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("ic.png", _make_png()), "user_id": USER},
            **_h())
        assert r.status_code == 201, r.json()
        body = r.json()
        assert body["status"] == "ok"
        assert body["document_path"].startswith(USER + "/") and body["document_path"].endswith(".enc")
        assert body["document_mime"] == "image/png"
        assert "T" in body["uploaded_at"]
        assert body["nric_encrypted"].startswith('{"ver":2')
        assert body["needs_review"] is False
        assert body["warnings"] == []
        assert json.loads(store[body["document_path"]].decode())["ver"] == 2

    def test_gender_cross_check_warns(self, store, ocr):
        r = Client().post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("ic.png", _make_png()),
                  "user_id": USER, "gender": "female"},  # serial ends 3 → male
            **_h())
        body = r.json()
        assert r.status_code == 201
        assert body["needs_review"] is True and body["warnings"]

    def test_mrz_passport_number_encrypted_not_discarded(self, store, monkeypatch):
        """A valid TD3 MRZ passport must encrypt the MRZ passport number into
        nric_encrypted (the profile ic_encrypted column receives it). With
        needs_review False (OCR confidence ok) a discarded number would leave
        the document stored with NO id number and no reviewer flag."""
        fake = MagicMock()
        fake.ocr.return_value = [{
            "rec_texts": [
                "P<MYSTAN<<MEI<LING<<<<<<<<<<<<<<<<<<<<<<<<<<",
                "A123456784MYS8501020F3001318ZE184736B<<<<<30",
            ],
            "rec_scores": [0.95, 0.95],
        }]
        monkeypatch.setattr(services, "_get_ocr", lambda: fake)
        r = Client().post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("pp.png", _make_png()), "user_id": USER},
            **_h())
        body = r.json()
        assert r.status_code == 201, body
        assert body["nric_encrypted"], body
        assert json.loads(body["nric_encrypted"])["ver"] == 2
        assert body["needs_review"] is False


@pytest.mark.security
class TestEncryptIcEndpoint:
    def _post(self, payload):
        return Client().post("/ai/documents/encrypt-ic", data=json.dumps(payload),
                             content_type="application/json", **_h())

    def test_valid_nric_201(self, store):
        r = self._post({"ic_number": "010101-14-5123", "user_id": USER, "dob": "2001-01-01"})
        body = r.json()
        assert r.status_code == 201 and body["status"] == "ok"
        assert body["needs_review"] is False
        assert json.loads(body["ic_encrypted"])["ver"] == 2

    def test_dob_mismatch_hard_reject(self, store):
        r = self._post({"ic_number": "010101-14-5123", "user_id": USER, "dob": "2001-01-02"})
        assert r.status_code == 422

    def test_invalid_state_reject(self, store):
        r = self._post({"ic_number": "010101-18-5123", "user_id": USER})
        assert r.status_code == 422

    def test_all_zero_serial_reject(self, store):
        r = self._post({"ic_number": "010101-14-0000", "user_id": USER})
        assert r.status_code == 422

    def test_passport_loose_format_needs_review(self, store):
        r = self._post({"ic_number": "A1234567", "user_id": USER})
        body = r.json()
        assert r.status_code == 201 and body["needs_review"] is True

    def test_passport_bad_format_reject(self, store):
        assert self._post({"ic_number": "A12*67", "user_id": USER}).status_code == 422

    def test_passport_too_long_reject(self, store):
        assert self._post({"ic_number": "A1234567890123456", "user_id": USER}).status_code == 422

    def test_non_string_dob_400(self, store):
        assert self._post({"ic_number": "010101-14-5123", "user_id": USER, "dob": 20010101}).status_code == 400

    def test_non_string_gender_400(self, store):
        assert self._post({"ic_number": "010101-14-5123", "user_id": USER, "gender": ["male"]}).status_code == 400


@pytest.mark.security
class TestDeleteEndpoint:
    def test_delete_own_blob(self, store):
        path = f"{USER}/1738712345000.enc"
        store[path] = b"x"
        r = Client().post("/ai/documents/delete",
                          data=json.dumps({"user_id": USER, "encrypted_path": path}),
                          content_type="application/json", **_h())
        assert r.json() == {"deleted": True}
        assert path not in store

    def test_idor_other_user_403(self, store):
        path = f"{OTHER}/1738712345000.enc"
        r = Client().post("/ai/documents/delete",
                          data=json.dumps({"user_id": USER, "encrypted_path": path}),
                          content_type="application/json", **_h())
        assert r.status_code == 403

    def test_traversal_403(self, store):
        r = Client().post("/ai/documents/delete",
                          data=json.dumps({"user_id": USER, "encrypted_path": f"{USER}/../{OTHER}/1.enc"}),
                          content_type="application/json", **_h())
        assert r.status_code == 403

    def test_missing_user_id_400(self, store):
        r = Client().post("/ai/documents/delete",
                          data=json.dumps({"encrypted_path": f"{USER}/1.enc"}),
                          content_type="application/json", **_h())
        assert r.status_code == 400


@pytest.mark.security
class TestDecryptRouteDeleted:
    def test_decrypt_route_gone(self):
        r = Client().post("/ai/documents/decrypt",
                          data=json.dumps({"encrypted_path": "x", "iv": "x", "tag": "x",
                                           "user_id": USER}),
                          content_type="application/json", **_h())
        assert r.status_code == 404


@pytest.mark.security
class TestThrottle:
    def test_scoped_throttle_blocks_after_10_per_hour(self, store, ocr):
        c = Client(HTTP_X_USER_ID=USER)
        statuses = []
        for _ in range(11):
            r = c.post("/ai/documents/encrypt",
                       data={"document": SimpleUploadedFile("ic.png", _make_png()),
                             "user_id": USER},
                       **_h())
            statuses.append(r.status_code)
        assert statuses[:10] == [201] * 10, statuses
        assert statuses[10] == 429

    def test_other_user_not_affected(self, store, ocr):
        c1 = Client(HTTP_X_USER_ID=USER)
        for _ in range(11):
            c1.post("/ai/documents/encrypt",
                    data={"document": SimpleUploadedFile("ic.png", _make_png()),
                          "user_id": USER}, **_h())
        r = Client(HTTP_X_USER_ID=OTHER).post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("ic.png", _make_png()), "user_id": OTHER},
            **_h())
        assert r.status_code == 201


@pytest.mark.security
class TestMiddlewareFailClosed:
    def test_missing_header_401(self):
        r = Client().post("/ai/documents/encrypt", data={"user_id": USER})
        assert r.status_code == 401
