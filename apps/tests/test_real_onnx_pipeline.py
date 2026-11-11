"""
REAL (non-mocked) ONNX photo-pipeline integration tests — FR-16..19, NFR02,
NFR09.

No engine, threshold, or model mock: these tests load the real YOLO ONNX
export (models/best.onnx via onnxruntime) and the real RapidOCR
(PP-OCRv5 mobile ONNX) and run decode → detect → crop → OCR → extract →
rank → route on committed fixture photos.

Fixtures (copied from the staging sample set "bib photo/", sha256):
  - bib-sample-1.jpg  550485bcf1a32ef61f6d19795bd05cb3b0444f29f5bc053a82c9fb646e26c774
    (source: bib4.jpg — two readable bibs, staging parity read "3004"/"5073")
  - bib-sample-2.jpg  9f35699d44d0e5af0e20d8c63592a389692486f66b00e3784881901f3fbe42ba
    (source: bib5.jpg — one sharp bib, staging parity read "499")
  - not-an-image.bin  e9d6fe24813bbd563509a5586de90257995a0dc6d70425717c6c59f5e80ef46b
    (deterministic garbage blob b'not-an-image' * 64)

Real models are not perfectly deterministic across runtime versions (a 1px
detector swing can flip a digit read — see the parity note in
reader.read_bibs_from_image), so these tests assert RANGES, presence, and
threshold-matrix CONSISTENCY on measured values — never exact bib numbers
or fabricated confidences. Measured staging values (2026-11-12):
  bib-sample-1: YOLO conf 0.9058/0.8508, OCR "3004"@0.8947 / "5073"@1.0 → auto
  bib-sample-2: YOLO conf 0.8960, OCR "499"@0.9982 → auto
"""
import pathlib
import time

import pytest

from apps.ai_photos.reader import (
    decode_image,
    get_class_name,
    get_reader_options,
    get_yolo_model,
    is_bib_detection,
    read_bibs_from_bytes,
    status_for_confidence,
)
from apps.ai_photos.services import read_storage_photo

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


class _FakeStorage:
    """Storage-transport stub: returns canned bytes for any path."""

    def __init__(self, payload: bytes):
        self.payload = payload

    def from_(self, bucket):
        return self

    def download(self, path):
        return self.payload


class _FakeSb:
    def __init__(self, payload: bytes):
        self.storage = _FakeStorage(payload)


@pytest.mark.performance
class TestRealOnnxPipeline:
    """Real onnxruntime + RapidOCR engines, real settings thresholds."""

    def test_fr16_real_yolo_detects_bib(self):
        """FR-16: the real YOLO ONNX export finds bib boxes on a clear photo."""
        image = decode_image((FIXTURES / 'bib-sample-2.jpg').read_bytes())
        options = get_reader_options()
        model = get_yolo_model()

        detections = [
            d for d in model.predict(
                image, conf=options.yolo_conf, iou=options.yolo_iou,
                max_det=options.yolo_max_det,
            )
            if is_bib_detection(model, d['class_id'])
        ]
        assert len(detections) >= 1
        for detected in detections:
            assert 'bib' in get_class_name(model, detected['class_id'])
            assert 0.0 < detected['conf'] <= 1.0
            # box inside the source image
            height, width = image.shape[:2]
            x1, y1, x2, y2 = detected['xyxy']
            assert 0 <= x1 < x2 <= width
            assert 0 <= y1 < y2 <= height

    def test_fr17_real_ocr_extracts_bib_number(self):
        """FR-17: real crop → RapidOCR → extract_bib_number yields a readable bib.

        Asserts presence/shape, not the exact number: real models are not
        perfectly deterministic across runtime versions, so the exact
        digits may vary (staging parity measured "499" here).
        """
        result = read_bibs_from_bytes(
            (FIXTURES / 'bib-sample-2.jpg').read_bytes(),
            source_name='bib-sample-2.jpg',
        )
        bib_number = result['bib_number']
        assert isinstance(bib_number, str)
        assert 2 <= len(bib_number) <= 8
        assert any(char.isdigit() for char in bib_number)
        # the merged OCR confidence respects the (0, 1] score range
        assert 0.0 < result['confidence'] <= 1.0
        # at least one detection actually produced a read (not just boxes)
        assert any(d['bib_number'] for d in result['detections'])

    def test_fr18_clear_photo_routes_by_measured_confidence(self):
        """FR-18: the real pipeline's measured conf lands in the right bucket.

        The clear sample photo reads with OCR confidence at/above the
        review floor on staging (parity: 1.0 for "5073", 0.9982 for "499"),
        so a discarded outcome on it is a regression. The bucket itself is
        whatever the REAL measured confidence maps to via the real matrix
        (>=0.85 auto, else review) — never faked.
        """
        options = get_reader_options()
        result = read_bibs_from_bytes(
            (FIXTURES / 'bib-sample-1.jpg').read_bytes(),
            source_name='bib-sample-1.jpg',
        )
        measured = result['confidence']
        assert 0.0 < measured <= 1.0
        # a clear, readable photo is never discarded
        assert measured >= options.review_confidence
        # routing function maps the REAL measured conf to the correct bucket
        assert result['status'] == status_for_confidence(measured, options)

    def test_fr19_threshold_matrix_review_and_discard(self):
        """FR-19: [0.50, 0.85) → review, [0, 0.50) → discard — on the real
        matrix function AND the real measured confidence from the fixture
        photo (probe boundaries are exact matrix inputs, not mocks)."""
        options = get_reader_options()
        # the real settings matrix boundaries
        assert options.auto_confidence == 0.85
        assert options.review_confidence == 0.50

        # exact boundary probes against the real pure function
        assert status_for_confidence(1.0, options) == 'auto'
        assert status_for_confidence(options.auto_confidence, options) == 'auto'
        assert status_for_confidence(0.8499, options) == 'review'
        assert status_for_confidence(options.review_confidence, options) == 'review'
        assert status_for_confidence(0.4999, options) == 'discarded'
        assert status_for_confidence(0.0, options) == 'discarded'

        # the REAL measured conf from the fixture maps to the correct bucket
        result = read_bibs_from_bytes(
            (FIXTURES / 'bib-sample-2.jpg').read_bytes(),
            source_name='bib-sample-2.jpg',
        )
        measured = result['confidence']
        assert result['status'] == status_for_confidence(measured, options)

    def test_nfr02_real_pipeline_wall_clock(self):
        """NFR02: full REAL pass (decode → YOLO → crop → OCR → rank) stays
        under the 30s ceiling — the non-mocked complement of the mocked
        ceiling test. Cold-start (model load included) measured 6.0s and
        warm 0.4s on staging, both far below the ceiling."""
        payload = (FIXTURES / 'bib-sample-2.jpg').read_bytes()
        start = time.monotonic()
        result = read_bibs_from_bytes(payload, source_name='bib-sample-2.jpg')
        elapsed = time.monotonic() - start
        assert result['bib_number'], 'pipeline produced no read at all'
        assert elapsed < 30.0

    def test_nfr09_garbage_skipped_at_pipeline_boundary(self):
        """NFR09: garbage bytes are skipped gracefully — read_storage_photo
        catches the decode failure and returns an error dict instead of
        raising out of the pipeline boundary (worker keeps draining)."""
        garbage = (FIXTURES / 'not-an-image.bin').read_bytes()
        result = read_storage_photo('garbage/not-an-image.bin', sb=_FakeSb(garbage))
        assert result['storage_path'] == 'garbage/not-an-image.bin'
        assert 'Could not decode image bytes' in result['error']
