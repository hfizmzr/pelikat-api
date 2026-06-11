"""
Corrupt Image Handling Reliability Tests (NFR: AI pipeline handles corrupt images gracefully)
"""
import pytest
import io
from PIL import Image
import numpy as np
from unittest.mock import patch, MagicMock

from apps.ai_photos.reader import (
    decode_image,
    read_bibs_from_bytes,
    read_bibs_from_image,
    get_yolo_model,
    get_ocr_reader,
)
from apps.ai_photos.services import (
    read_storage_photo,
    process_photo,
    process_storage_paths,
)


@pytest.mark.reliability
class TestCorruptImageHandling:
    """Verify the AI pipeline handles corrupt images gracefully."""

    def test_truncated_png_raises_value_error(self):
        """Truncated PNG bytes should raise ValueError with clear message."""
        truncated_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'  # Incomplete PNG header
        
        with pytest.raises(ValueError, match="Could not decode image bytes"):
            decode_image(truncated_png)

    def test_random_bytes_raises_value_error(self):
        """Random bytes should raise ValueError with clear message."""
        random_bytes = b'\x00\x01\x02\x03\x04\x05' * 100
        
        with pytest.raises(ValueError, match="Could not decode image bytes"):
            decode_image(random_bytes)

    def test_empty_bytes_raises_value_error(self):
        """Empty bytes should raise an error (ValueError or cv2.error)."""
        with pytest.raises(Exception):
            decode_image(b'')

    def test_read_bibs_from_bytes_raises_on_invalid_data(self):
        """read_bibs_from_bytes with invalid data raises ValueError (caller should catch)."""
        invalid_bytes = b'not an image at all'
        
        with pytest.raises(ValueError, match="Could not decode image bytes"):
            read_bibs_from_bytes(invalid_bytes, source_name='corrupt.jpg')

    def test_read_storage_photo_graceful_error(self):
        """read_storage_photo catches errors and returns error dict."""
        with patch('apps.ai_photos.services.get_supabase_client') as mock_get_sb:
            mock_sb = MagicMock()
            # Simulate download failure
            mock_sb.storage.from_.return_value.download.side_effect = Exception("Download failed")
            mock_get_sb.return_value = mock_sb
            
            result = read_storage_photo('test.jpg')
            
            assert 'error' in result
            assert 'Download failed' in result['error']
            assert result['storage_path'] == 'test.jpg'

    def test_process_photo_skips_corrupt_images(self):
        """process_photo skips corrupt images without inserting them."""
        with patch('apps.ai_photos.services.get_supabase_client') as mock_get_sb:
            mock_sb = MagicMock()
            mock_get_sb.return_value = mock_sb
            
            with patch('apps.ai_photos.services.read_storage_photo') as mock_read:
                mock_read.return_value = {
                    'storage_path': 'corrupt.jpg',
                    'error': 'Could not decode image',
                }
                
                result = process_photo('corrupt.jpg', 'event-123', 'org-123', 'batch-456')
                
                # Should return error result without inserting
                assert 'error' in result
                # Should NOT delete/insert into photo_tags
                mock_sb.table.return_value.delete.assert_not_called()
                mock_sb.table.return_value.insert.assert_not_called()

    def test_batch_processing_continues_past_corrupt(self):
        """process_storage_paths continues processing even if one image is corrupt."""
        with patch('apps.ai_photos.services.get_supabase_client') as mock_get_sb:
            mock_sb = MagicMock()
            mock_get_sb.return_value = mock_sb
            
            with patch('apps.ai_photos.services.process_photo') as mock_process:
                # First image is corrupt, second is valid
                mock_process.side_effect = [
                    {'error': 'Corrupt image', 'storage_path': 'bad.jpg'},
                    {'saved_tags': 2, 'storage_path': 'good.jpg'},
                ]
                
                result = process_storage_paths(
                    ['bad.jpg', 'good.jpg'],
                    'event-123', 'org-123', 'batch-456'
                )
                
                assert result['processed'] == 2
                assert len(result['results']) == 2
                # First result is error
                assert 'error' in result['results'][0]
                # Second result is success
                assert result['results'][1]['saved_tags'] == 2

    def test_graceful_zero_detections(self):
        """Images with no bib detections are handled gracefully (discarded status)."""
        # Create a valid but empty image (no text/bib)
        img = Image.new('RGB', (100, 100), color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        image_bytes = buf.getvalue()
        
        # We mock the YOLO model to return zero detections
        mock_result = MagicMock()
        mock_result.boxes = []
        
        with patch('apps.ai_photos.reader.get_yolo_model') as mock_yolo:
            mock_model = MagicMock()
            mock_model.predict.return_value = [mock_result]
            mock_model.names = {0: 'bib'}
            mock_yolo.return_value = mock_model
            
            with patch('apps.ai_photos.reader.get_ocr_reader') as mock_ocr:
                mock_ocr.return_value = MagicMock()
                
                result = read_bibs_from_bytes(image_bytes, source_name='empty.jpg')
                
                assert result['status'] == 'discarded'
                assert result['bib_number'] is None
                assert len(result['detections']) == 0

    def test_invalid_image_format_handled(self):
        """Non-image files (e.g., text files renamed as .jpg) are handled gracefully."""
        fake_image = b'This is not an image but has .jpg extension'
        
        with pytest.raises(ValueError, match="Could not decode image bytes"):
            read_bibs_from_bytes(fake_image, source_name='fake.jpg')
