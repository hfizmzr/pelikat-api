"""
AI Photo Processing Service

Uses YOLO for bib detection and EasyOCR for OCR on cropped regions.
"""

import numpy as np
import cv2
from supabase import create_client
from django.conf import settings

from ultralytics import YOLO
import easyocr

model = None
reader = None
supabase_client = None


def get_supabase_client():
    global supabase_client
    if supabase_client is None:
        supabase_client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_ROLE_KEY
        )
    return supabase_client


def get_yolo_model():
    global model
    if model is None:
        model = YOLO('yolov8n.pt')
    return model


def get_ocr_reader():
    global reader
    if reader is None:
        reader = easyocr.Reader(['en'], gpu=False)
    return reader


def process_photo(storage_path: str, event_id: str, organizer_id: str, batch_id: str):
    """
    Download photo from Supabase, run YOLO -> crop bib region -> OCR -> write photo_tags.
    """
    sb = get_supabase_client()
    yolo = get_yolo_model()
    ocr_reader = get_ocr_reader()

    try:
        res = sb.storage.from_('race-photos').download(storage_path)
        img_bytes = res

        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        results = yolo(img)

        bib_number = None
        confidence = 0.0

        for box in results[0].boxes:
            if box.conf[0] > 0.5:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                crop = img[y1:y2, x1:x2]

                ocr_result = ocr_reader.readtext(crop)
                if ocr_result:
                    text = ocr_result[0][1].strip().upper()
                    conf = float(ocr_result[0][2])
                    if conf > confidence:
                        bib_number = text
                        confidence = conf

        if confidence > 0.85:
            status = 'auto'
        elif confidence >= 0.50:
            status = 'review'
        else:
            status = 'discarded'

        sb.table('photo_tags').insert({
            'event_id': event_id,
            'organizer_id': organizer_id,
            'storage_path': storage_path,
            'bib_number': bib_number,
            'confidence': confidence,
            'status': status,
            'batch_id': batch_id,
        }).execute()

        return {'storage_path': storage_path, 'bib_number': bib_number, 'confidence': confidence, 'status': status}

    except Exception as e:
        return {'storage_path': storage_path, 'error': str(e)}


def get_batch_status(batch_id: str) -> dict:
    """Get processing status for a batch."""
    sb = get_supabase_client()

    res = sb.table('photo_tags').select('status').eq('batch_id', batch_id).execute()

    counts = {'auto': 0, 'review': 0, 'discarded': 0, 'pending': 0}
    total = len(res.data)
    for row in res.data:
        status = row.get('status', 'pending')
        counts[status] = counts.get(status, 0) + 1

    counts['pending'] = total
    return {'batch_id': batch_id, 'total': total, 'counts': counts}