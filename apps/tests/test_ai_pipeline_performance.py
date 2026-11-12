"""Covers: NFR02 — AI pipeline ≤ 30 s/photo (UC05 AI Photos).
Layers: mocked pipeline timing + wall-clock 30 s ceiling test. Tests: 7.

AI Pipeline Performance Tests (NFR: AI pipeline < 30s per photo on standard CPU)
"""
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from apps.ai_photos.reader import (
    decode_image,
    get_reader_options,
    read_bibs_from_bytes,
)


@pytest.mark.performance
class TestAiPipelinePerformance:
    """Verify AI pipeline performance constraints."""

    def test_decode_image_fast(self):
        """Decoding a valid JPEG shall take < 100ms."""
        # Create a minimal valid JPEG (simple 1x1 pixel)
        import io

        from PIL import Image
        img = Image.new('RGB', (1, 1), color='red')
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        image_bytes = buf.getvalue()
        
        start = time.perf_counter()
        decoded = decode_image(image_bytes)
        elapsed = time.perf_counter() - start
        
        assert decoded is not None
        assert elapsed < 0.1, f"Image decode took {elapsed:.3f}s, exceeds 100ms"

    def test_corrupt_image_rejected_fast(self):
        """Corrupt/truncated images shall be rejected within 5s (fast-fail)."""
        corrupt_bytes = b'\x89PNG\r\n\x1a\ninvalid_truncated_data'
        
        start = time.perf_counter()
        with pytest.raises(ValueError, match="Could not decode image bytes"):
            decode_image(corrupt_bytes)
        elapsed = time.perf_counter() - start
        
        assert elapsed < 5.0, f"Corrupt image rejection took {elapsed:.3f}s, exceeds 5s"

    def test_random_bytes_rejected_fast(self):
        """Random bytes (not an image) shall be rejected within 5s."""
        random_bytes = b'\x00\x01\x02\x03\x04\x05' * 1000
        
        start = time.perf_counter()
        with pytest.raises(ValueError, match="Could not decode image bytes"):
            decode_image(random_bytes)
        elapsed = time.perf_counter() - start
        
        assert elapsed < 5.0, f"Random bytes rejection took {elapsed:.3f}s, exceeds 5s"

    def test_read_bibs_from_bytes_graceful_on_error(self):
        """read_bibs_from_bytes with invalid data raises ValueError (caught upstream by read_storage_photo)."""
        invalid_bytes = b'not an image at all'
        
        with pytest.raises(ValueError, match="Could not decode image bytes"):
            read_bibs_from_bytes(invalid_bytes, source_name='test.jpg')

    def test_reader_options_defaults(self):
        """Reader options have sensible defaults for performance."""
        opts = get_reader_options()

        assert opts.yolo_conf == 0.05
        assert opts.yolo_imgsz == 1280
        assert opts.yolo_max_det == 50  # Reasonable max detections per image
        
    def test_padded_crop_efficiency(self):
        """padded_crop with zero padding should be fast (< 10ms)."""
        from apps.ai_photos.reader import padded_crop
        
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        xyxy = [10, 10, 50, 50]
        
        start = time.perf_counter()
        crop, _ = padded_crop(image, xyxy, 0.0)
        elapsed = time.perf_counter() - start
        
        assert crop is not None
        assert elapsed < 0.01, f"Padded crop took {elapsed:.3f}s, exceeds 10ms"

    def test_pipeline_within_30s_nfr02_ceiling(self):
        """NFR02: the full per-photo pipeline path shall stay within a 30s ceiling per photograph.

        FYP NFR: "AI photo matching... within 30 seconds per photograph on target
        hardware." This is a ceiling assertion only — sub-second targets are the
        job of the fast proxy tests above (test_decode_image_fast,
        test_padded_crop_efficiency). The YOLO/OCR engines are mocked (same
        pattern as test_corrupt_image_handling.py) so the timed path is decode →
        predict → crop → OCR → bib extraction/rank → in-memory BibIndex match,
        with no model download or ONNX inference.
        """
        import io

        from PIL import Image

        from apps.ai_photos.services import BibIndex

        # Reuse the file's synthetic small image fixture (valid JPEG bytes).
        img = Image.new('RGB', (100, 100), color='white')
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        image_bytes = buf.getvalue()

        with patch('apps.ai_photos.reader.get_yolo_model') as mock_yolo:
            mock_model = MagicMock()
            mock_model.names = {1: 'bib'}
            mock_model.predict.return_value = [
                {'class_id': 1, 'conf': 0.9, 'xyxy': [10, 10, 90, 90]}
            ]
            mock_yolo.return_value = mock_model

            with patch('apps.ai_photos.reader.get_ocr_reader') as mock_ocr:
                mock_reader = MagicMock()
                mock_reader.ocr.return_value = [{
                    'rec_texts': ['5073'],
                    'rec_scores': [1.0],
                    'rec_polys': [np.array(
                        [[5, 20], [40, 20], [40, 50], [5, 50]], dtype=float)],
                }]
                mock_ocr.return_value = mock_reader

                # Mocked per-photo pipeline invocation: decode → OCR → rank → match.
                bib_index = BibIndex([{'id': 'reg-uuid', 'bib_number': '5073'}])
                start = time.monotonic()
                result = read_bibs_from_bytes(image_bytes, source_name='nfr02.jpg')
                matched = bib_index.find(result['bib_number'])
                elapsed = time.monotonic() - start

        assert result['bib_number'] == '5073'  # pipeline produced a readable bib
        assert matched is not None  # bib matched a registration
        assert elapsed < 30.0, f"Pipeline took {elapsed:.3f}s, exceeds NFR02 30s ceiling"
