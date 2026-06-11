"""
AI Pipeline Performance Tests (NFR: AI pipeline < 30s per photo on standard CPU)
"""
import time
import pytest
import numpy as np
from unittest.mock import patch, MagicMock

from apps.ai_photos.reader import (
    decode_image,
    read_bibs_from_bytes,
    read_bibs_from_image,
    get_reader_options,
    ReaderOptions,
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
        assert opts.upscale_factor == 3
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
