"""Covers: FR-16 — YOLO bib-region decoder (letterbox → NMS → coords).
Layers: synthetic-output unit + committed-model smoke. Tests: 10.

Unit tests for the ONNX YOLO output decoder (letterbox → NMS →
coord mapping) on synthetic (1, 4+nc, N) outputs. No model files needed.
"""
import numpy as np
import pytest

from apps.ai_photos.onnx_yolo import (
    OnnxDetector,
    OnnxModelError,
    decode_end2end_output,
    decode_yolo_output,
    letterbox,
)

NAMES = {0: "bib"}
IOU = 0.50


def synth_output(anchors, n_rows=8400, nc=1, input_size=640):
    """Build a (1, 4+nc, n_rows) raw detect output.

    `anchors`: iterable of (cx, cy, w, h, conf) in letterboxed
    input-pixel coordinates. The rest of the N anchors are zero-scored.
    """
    columns = [list(map(float, a[:4 + nc])) for a in anchors]
    while len(columns) < n_rows:
        columns.append([0.0, 0.0, 1.0, 1.0, 0.0])
    return np.array(columns, dtype=np.float32).T[None], input_size


def decode(output, *, input_size=640, **kw):
    """Decode with the (scale, pads) that letterbox() produced for a
    320x480 image fed into a `input_size`-square input."""
    img = np.zeros((480, 320, 3), dtype=np.uint8)
    box, (scale, pad_x, pad_y) = letterbox(img, input_size)
    assert box.shape[:2] == (input_size, input_size)
    return decode_yolo_output(
        output, NAMES, scale, pad_x, pad_y,
        conf_threshold=0.05, iou_threshold=kw.pop("iou", IOU),
        max_det=kw.pop("max_det", 50),
    )


class TestLetterbox:
    """Letterbox pads to square and scales to fit."""

    def test_pads_to_square_with_gray(self):
        img = np.full((480, 320, 3), 200, dtype=np.uint8)
        padded, info = letterbox(img, 640)
        assert padded.shape[:2] == (640, 640)
        scale, pad_left, pad_top = info
        # upscales 320x480 by 4/3 to 427x640, centers on x with continuous pad
        assert abs(scale - 4 / 3) < 1e-9
        assert pad_left == pytest.approx((640 - 427) / 2, abs=0.51)
        assert pad_top == 0
        # outer border painted 114, original pixels kept
        assert tuple(padded[0, 0]) == (114, 114, 114)
        assert tuple(padded[100, 300]) == (200, 200, 200)

    def test_scales_to_fit(self):
        img = np.zeros((960, 1280, 3), dtype=np.uint8)
        padded, (scale, _, _) = letterbox(img, 640)
        assert padded.shape[:2] == (640, 640)
        assert abs(scale - 640 / 1280) < 1e-9


class TestDecodeRoundtrip:
    """Decoder roundtrip on synthetic YOLO outputs (NMS, order, bad shapes)."""

    def test_synthetic_8400_rows_roundtrip_and_nms(self):
        # Image 320x480 → scale 4/3 (upscale allowed, as in predict),
        # continuous pad_left (640-427)/2 ≈ 106.5, pad_top 0.
        img = np.zeros((480, 320, 3), dtype=np.uint8)
        _, (scale, pad_left, pad_top) = letterbox(img, 640)

        def to_input_space(x, y, w, h):  # original → letterboxed anchor
            return ((pad_left + (x + w / 2) * scale), (pad_top + (y + h / 2) * scale),
                    w * scale, h * scale)

        # Original-image box (100, 200)–(200, 360).
        good = (*to_input_space(100, 200, 100, 160), 0.9)
        # Duplicate of good inside the iou threshold → must be suppressed.
        near_dup = (*to_input_space(101, 201, 100, 160), 0.8)
        # Below conf threshold → filtered.
        weak = (*to_input_space(10, 10, 40, 40), 0.01)
        output, _ = synth_output([good, near_dup, weak])
        dets = decode_yolo_output(
            output, NAMES, scale, pad_left, pad_top,
            conf_threshold=0.05, iou_threshold=IOU, max_det=50,
        )

        assert len(dets) == 1
        det = dets[0]
        assert det["class_id"] == 0
        assert det["conf"] == pytest.approx(0.9, abs=1e-6)
        x1, y1, x2, y2 = det["xyxy"]
        # Mapped back into original image coordinates.
        assert x1 == pytest.approx(100, abs=1.0)
        assert y1 == pytest.approx(200, abs=1.0)
        assert x2 == pytest.approx(200, abs=1.0)
        assert y2 == pytest.approx(360, abs=1.0)

    def test_scaled_image_roundtrip(self):
        # A 640x960 image in a 640-square: scale 2/3 (downscale), x pads.
        img = np.zeros((960, 640, 3), dtype=np.uint8)
        _, (scale, pad_left, pad_top) = letterbox(img, 640)
        assert abs(scale - 2 / 3) < 1e-9
        assert pad_top == 0
        assert pad_left == pytest.approx((640 - 427) / 2, abs=0.51)
        # Original box (100, 200)–(300, 400) in to-input-space.
        anchor = (pad_left + 200 * scale, 300 * scale,
                  200 * scale, 200 * scale, 0.7)
        output, _ = synth_output([anchor])
        dets = decode_yolo_output(
            output, NAMES, scale, pad_left, pad_top,
            conf_threshold=0.05, iou_threshold=IOU, max_det=50,
        )
        assert len(dets) == 1
        assert dets[0]["xyxy"] == pytest.approx([100, 200, 300, 400], abs=1.0)

    def test_max_det_and_desc_order(self):
        anchors = [
            (40 + 200 * i, 40, 80, 80, 0.5 + i / 10)
            for i in range(5)  # five far-apart boxes, confs 0.5..0.9
        ]
        output, _ = synth_output(anchors)
        dets = decode(output, max_det=2)
        assert [round(d["conf"], 3) for d in dets] == [0.9, 0.8]

    def test_empty_and_bad_shapes(self):
        output, _ = synth_output([(310, 360, 100, 160, 0.9)])
        with pytest.raises(ValueError):
            decode_yolo_output(
                output[0], NAMES, 1.0, 160, 80,
                conf_threshold=0.5, iou_threshold=IOU, max_det=50,
            )
        zs = np.zeros((1, 5, 10), dtype=np.float32)
        # conf 0.05: everything zero-scored → empty list
        assert decode_yolo_output(
            zs, NAMES, 1.0, 0, 0,
            conf_threshold=0.05, iou_threshold=IOU, max_det=50,
        ) == []
        # name/class-row mismatch is a model error, not silence
        with pytest.raises(OnnxModelError):
            decode_yolo_output(
                np.zeros((1, 6, 10), dtype=np.float32), NAMES, 1.0, 0, 0,
                conf_threshold=0.05, iou_threshold=IOU, max_det=50,
            )


class TestEnd2End:
    """NMS-free (1, K, 6) export — what YOLO26 best.pt predicts with."""

    def test_threshold_sort_maxdet_and_mapping(self):
        rows = np.zeros((1, 300, 6), dtype=np.float32)
        rows[0, 0] = [100, 120, 200, 220, 0.40, 0]
        rows[0, 1] = [10, 20, 30, 40, 0.90, 0]
        rows[0, 2] = [5, 5, 6, 6, 0.01, 0]  # below conf, dropped
        out = decode_end2end_output(rows, 2.0, 10.0, 20.0, conf_threshold=0.05, max_det=50)
        assert [d["conf"] for d in out] == pytest.approx([0.90, 0.40])
        assert out[0]["xyxy"] == pytest.approx([0.0, 0.0, 10.0, 10.0])
        assert out[1]["xyxy"] == pytest.approx([45.0, 50.0, 95.0, 100.0])
        assert decode_end2end_output(rows, 1, 0, 0, conf_threshold=0.05, max_det=1)[0]["conf"] == pytest.approx(0.9)

    def test_bad_shape(self):
        with pytest.raises(ValueError):
            decode_end2end_output(np.zeros((1, 5, 300), np.float32), 1, 0, 0, conf_threshold=0.05, max_det=5)

    def test_committed_model_is_end2end(self):
        from django.conf import settings
        det = OnnxDetector(settings.AI_PHOTOS_YOLO_MODEL_PATH)
        assert det.end2end is True
        assert det.imgsz == 1280

    def test_feeds_rgb(self, monkeypatch):
        """cv2 gives BGR; the model must see RGB like ultralytics feeds it."""
        from django.conf import settings
        det = OnnxDetector(settings.AI_PHOTOS_YOLO_MODEL_PATH)
        seen = {}

        class FakeSession:
            def run(self, _names, feeds):
                seen["blob"] = next(iter(feeds.values()))
                return [np.zeros((1, 300, 6), np.float32)]

        monkeypatch.setattr(det, "session", FakeSession())
        img = np.zeros((det.imgsz, det.imgsz, 3), np.uint8)
        img[..., 0] = 255  # pure blue in BGR
        assert det.predict(img) == []
        blob = seen["blob"]
        assert blob[0, 2].mean() == pytest.approx(1.0)  # blue lands in channel 2 (RGB)
        assert blob[0, 0].mean() == pytest.approx(0.0)
