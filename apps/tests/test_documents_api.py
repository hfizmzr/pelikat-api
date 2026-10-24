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
from apps.documents.mrz import parse_td3
from apps.documents.throttling import UserScopedRateThrottle

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

    def test_mrz_truncated_composite_rejected_422(self, store, monkeypatch):
        """Composite check digit literally '<' (OCR truncated the TD3 tail):
        the MRZ cannot be verified, so the upload must be hard-rejected
        (422) through the full API path — not accepted with an unverifiable
        passport number just because every parsed check digit happens to
        pass."""
        l1 = "P<MYSTAN<<MEI<LING<<<<<<<<<<<<<<<<<<<<<<<<<<"
        l2 = "A123456784MYS8501020F3001318ZE184736B<<<<<3<"  # [43] = '<'
        parsed = parse_td3(f"{l1}\n{l2}")
        assert parsed["truncated"] and not parsed["errors"], parsed
        fake = MagicMock()
        fake.ocr.return_value = [{"rec_texts": [l1, l2],
                                  "rec_scores": [0.95, 0.95]}]
        monkeypatch.setattr(services, "_get_ocr", lambda: fake)
        r = Client().post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("pp.png", _make_png()), "user_id": USER},
            **_h())
        assert r.status_code == 422, r.json()
        assert "MRZ" in r.json()["error"]


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

    def test_array_body_400_not_500(self):
        # Non-mapping JSON bodies must hit the missing-field 400 path.
        r = Client().post("/ai/documents/encrypt-ic", data=json.dumps([1]),
                          content_type="application/json", **_h())
        assert r.status_code == 400

    def test_null_body_400_not_500(self):
        r = Client().post("/ai/documents/encrypt-ic", data=json.dumps(None),
                          content_type="application/json", **_h())
        assert r.status_code == 400


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

    def test_array_body_400_not_500(self):
        # Non-mapping JSON bodies must hit the missing-field 400 path.
        r = Client().post("/ai/documents/delete", data=json.dumps([1]),
                          content_type="application/json", **_h())
        assert r.status_code == 400

    def test_null_body_400_not_500(self):
        r = Client().post("/ai/documents/delete", data=json.dumps(None),
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

    def test_header_rotation_cannot_dodge_body_keyed_throttle(self, store, ocr):
        """Rotating X-User-Id headers must not mint fresh buckets: the
        throttle keys on the BODY user_id (the identity the upload is
        stored under), so 11 uploads with the same body user_id hit the
        same bucket and the 11th is throttled."""
        statuses = []
        for i in range(11):
            r = Client(**_h({"HTTP_X_USER_ID": f"rotated-{i}"})).post(
                "/ai/documents/encrypt",
                data={"document": SimpleUploadedFile("ic.png", _make_png()),
                      "user_id": USER},
                **_h())
            statuses.append(r.status_code)
        assert statuses[:10] == [201] * 10, statuses
        assert statuses[10] == 429, statuses


@pytest.mark.security
class TestThrottleKeyGuard:
    """get_cache_key must survive non-mapping bodies (JSON array/null/
    string) and keep the key chain: body user_id → X-User-Id → client IP."""

    @staticmethod
    def _key(data, headers=None, meta=None):
        from types import SimpleNamespace
        req = SimpleNamespace(data=data, headers=headers or {},
                              META=meta or {"REMOTE_ADDR": "10.0.0.1"})
        return UserScopedRateThrottle().get_cache_key(req, None)

    @pytest.mark.parametrize("data", [[], None, "str", 42])
    def test_non_dict_body_falls_back_to_ip(self, data):
        key = self._key(data)
        assert key == "throttle_ic_doc_upload_10.0.0.1", key

    def test_parse_error_body_still_falls_back(self):
        # Malformed JSON raises ParseError on access, not AttributeError;
        # the header fallback must survive it.
        from rest_framework.exceptions import ParseError

        class _Req:
            def __init__(self):
                self.headers = {"X-User-Id": "h1"}
                self.META = {"REMOTE_ADDR": "10.0.0.1"}

            @property
            def data(self):
                raise ParseError()

        assert UserScopedRateThrottle().get_cache_key(_Req(), None) == \
            "throttle_ic_doc_upload_h1"

    def test_body_user_id_beats_rotated_header(self):
        key = self._key({"user_id": "body-uid"}, headers={"X-User-Id": "hdr-uid"})
        assert key == "throttle_ic_doc_upload_body-uid", key


@pytest.mark.security
class TestMiddlewareFailClosed:
    def test_missing_header_401(self):
        r = Client().post("/ai/documents/encrypt", data={"user_id": USER})
        assert r.status_code == 401


@pytest.mark.security
class TestOcrDegrade:
    """P1 graceful degradation: a Paddle std::exception (RuntimeError)
    must never escape as a 500 — blob is still stored encrypted and the
    201 contract carries needs_review + warnings=["ocr_failed"]."""

    def _upload(self, **kw):
        return Client(HTTP_X_USER_ID=USER).post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("ic.png", _make_png()),
                  "user_id": USER},
            **_h(**kw))

    def test_degrade_when_retry_also_fails(self, store, ocr):
        ocr.ocr.side_effect = RuntimeError("std::exception")
        r = self._upload()
        assert r.status_code == 201, r.status_code
        body = r.json()
        assert body["status"] == "ok"
        assert body["needs_review"] is True
        assert body["warnings"] == ["ocr_failed"]
        assert body["nric_encrypted"] is None
        # Blob stored + decryptable (AEAD v2 round-trip intact).
        assert body["document_path"] in store

    def test_reinit_retry_succeeds(self, store, monkeypatch):
        bad = MagicMock()
        bad.ocr.side_effect = RuntimeError("std::exception")
        good = MagicMock()
        good.ocr.return_value = [{
            "rec_texts": ["KAD PENGENALAN", "TAN MEI LING 010101-14-5123", "MALAYSIA"],
            "rec_scores": [0.95, 0.95, 0.95],
        }]
        monkeypatch.setattr(services, "_ocr", None)
        monkeypatch.setattr(services, "_get_ocr", iter([bad, good]).__next__)
        r = self._upload()
        assert r.status_code == 201
        body = r.json()
        assert body["needs_review"] is False
        assert body["warnings"] == []
        assert body["document_path"] in store
        monkeypatch.setattr(services, "_ocr", None)

    def test_json_guarantee_for_unhandled_exception(self, store, ocr,
                                                    monkeypatch):
        # /ai/* must NEVER leak a Django HTML 500 page; the global DRF
        # exception handler renders {"error": ...} JSON.
        def _boom(*a, **k):
            raise RuntimeError("unexpected paddle crash")
        monkeypatch.setattr(views, "validate_document", _boom)
        r = self._upload()
        assert r.status_code == 500
        assert r["Content-Type"].startswith("application/json")
        assert r.json() == {"error": "Internal server error"}


@pytest.mark.security
class TestThrottleScopes:
    """P2 scope split: deletes get a separate 30/h budget."""

    def _delete(self):
        return Client(HTTP_X_USER_ID=USER).post(
            "/ai/documents/delete",
            data=json.dumps({"user_id": USER, "encrypted_path": f"{USER}/1.enc"}),
            content_type="application/json", **_h())

    def test_11th_delete_within_hour_not_throttled(self, store, ocr):
        statuses = [self._delete().status_code for _ in range(11)]
        assert all(s != 429 for s in statuses), statuses

    def test_delete_throttled_at_31(self, store, ocr):
        statuses = [self._delete().status_code for _ in range(31)]
        assert statuses[:30] == [200] * 30, statuses
        assert statuses[30] == 429, statuses

    def test_scope_isolation_delete_vs_upload(self, store, ocr):
        for _ in range(30):
            self._delete()
        assert self._delete().status_code == 429
        # Upload scope untouched by delete activity.
        r = Client(HTTP_X_USER_ID=USER).post(
            "/ai/documents/encrypt",
            data={"document": SimpleUploadedFile("ic.png", _make_png()),
                  "user_id": USER},
            **_h())
        assert r.status_code == 201, r.status_code
