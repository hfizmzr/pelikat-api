"""
Unit tests for the RapidOCR line-merge and bib extraction path: RapidOCR's
PP-OCRv5 detector splits single bib numbers ("5073" → "507" + "73") that
the old Paddle pipeline read as one line — merge_ocr_lines recovers them.
"""
import numpy as np
import pytest

from apps.ai_photos.reader import build_bib_regex, extract_bib_number, merge_ocr_lines


def line(x1, y1, x2, y2, text, conf):
    return (np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=float), text, conf)


PAGE_SINGLE = [{"rec_texts": ["5073"], "rec_scores": [1.0],
                "rec_polys": [np.array([[5, 20], [80, 20], [80, 60], [5, 60]], dtype=float)]}]
PAGE_SPLIT = [{"rec_texts": ["507", "73"],
               "rec_scores": [1.0, 1.0],
               "rec_polys": [np.array([[5, 20], [50, 20], [50, 60], [5, 60]], dtype=float),
                             np.array([[56, 20], [80, 20], [80, 60], [56, 60]], dtype=float)]}]
PAGE_STACKED = [{"rec_texts": ["507", "32"], "rec_scores": [0.9, 0.9],
                 "rec_polys": [np.array([[5, 0], [50, 0], [50, 40], [5, 40]], dtype=float),
                               np.array([[5, 60], [50, 60], [50, 100], [5, 100]], dtype=float)]}]


class TestMergeOcrLines:
    def test_passes_single_line_through(self):
        out = merge_ocr_lines([(p, t, c) for p, t, c in
                               zip(PAGE_SINGLE[0]["rec_polys"], PAGE_SINGLE[0]["rec_texts"], PAGE_SINGLE[0]["rec_scores"])])
        assert len(out) == 1
        assert out[0][1] == "5073"

    def test_empty_input(self):
        assert merge_ocr_lines([]) == []

    def test_side_by_side_lines_merge(self):
        out = merge_ocr_lines(
            (p, t, c) for p, t, c in
            zip(PAGE_SPLIT[0]["rec_polys"], PAGE_SPLIT[0]["rec_texts"], PAGE_SPLIT[0]["rec_scores"]))

        assert out[0][1] == "50773"  # merged row reads left→right
        # parts kept alongside, nothing lost
        assert sorted(t for _, t, _ in out) == ["507", "50773", "73"]
        assert out[0][2] == pytest.approx(1.0)

    def test_stacked_lines_do_not_merge(self):
        out = merge_ocr_lines(
            (p, t, c) for p, t, c in
            zip(PAGE_STACKED[0]["rec_polys"], PAGE_STACKED[0]["rec_texts"], PAGE_STACKED[0]["rec_scores"]))

        assert sorted(t for _, t, _ in out) == ["32", "507"]


class TestExtractBibNumberMerges:
    def test_split_digits_recover_full_number(self):
        candidate = extract_bib_number(PAGE_SPLIT, build_bib_regex(), 0.5)
        assert candidate["bib_number"] == "50773"
        assert candidate["ocr_confidence"] == pytest.approx(1.0)

    def test_single_line_number_still_wins(self):
        candidate = extract_bib_number(PAGE_SINGLE, build_bib_regex(), 0.5)
        assert candidate["bib_number"] == "5073"

    def test_below_conf_lines_are_skipped(self):
        page = [{"rec_texts": ["507", "73"], "rec_scores": [0.1, 0.1],
                 "rec_polys": PAGE_SPLIT[0]["rec_polys"]}]
        assert extract_bib_number(page, build_bib_regex(), 0.5) is None
