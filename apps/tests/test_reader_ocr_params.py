"""Covers: FR-16/FR-17 — RapidOCR wiring for bib crops.
Layers: unit (engine call mocked). Tests: 4.

- RapidOCR 3.3.1 re-applies its own text_score/box_thresh/unclip_ratio
  defaults on every __call__, so RapidOcrEngine.ocr() must pass the
  Paddle-matched values per call (CodeRabbit PR #26).
- preprocess_for_ocr keeps the x3 upscale for normal crops but caps the
  upscaled size so a huge box cannot blow the worker's memory.
"""
import numpy as np

from apps.ai_photos import reader as R


class _FakeResult:
    boxes = None
    txts = ()
    scores = ()


class _FakeRapid:
    def __init__(self):
        self.calls = []

    def __call__(self, image, **kwargs):
        self.calls.append(kwargs)
        return _FakeResult()


def test_ocr_passes_paddle_thresholds_on_every_call():
    engine = R.RapidOcrEngine.__new__(R.RapidOcrEngine)
    engine.engine = _FakeRapid()
    img = np.zeros((40, 80, 3), np.uint8)
    assert engine.ocr(img) == []
    assert engine.ocr(img) == []
    expected = {
        'text_score': R.PADDLE_DET_PARAMS['Global.text_score'],
        'box_thresh': R.PADDLE_DET_PARAMS['Det.box_thresh'],
        'unclip_ratio': R.PADDLE_DET_PARAMS['Det.unclip_ratio'],
    }
    assert engine.engine.calls == [expected, expected]
    assert expected == {'text_score': 0.0, 'box_thresh': 0.6, 'unclip_ratio': 1.5}


def test_preprocess_keeps_x3_for_normal_crops():
    out = R.preprocess_for_ocr(np.full((120, 200, 3), 200, np.uint8), 3)
    assert out.shape == (360, 600, 3)


def test_preprocess_caps_huge_crops():
    # Already past the cap: no upscale at all (never shrunk either —
    # RapidOCR's own max_side_len handles oversized inputs).
    out = R.preprocess_for_ocr(np.full((3000, 4000, 3), 200, np.uint8), 3)
    assert out.shape[:2] == (3000, 4000)


def test_preprocess_partial_upscale_between():
    out = R.preprocess_for_ocr(np.full((500, 1000, 3), 200, np.uint8), 3)
    assert max(out.shape[:2]) == R.OCR_UPSCALE_MAX_SIDE  # x2, not x3
    assert out.shape[2] == 3
