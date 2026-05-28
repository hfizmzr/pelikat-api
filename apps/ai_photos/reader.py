"""
Bib reader for AI photo tagging.

Runs YOLO to locate bib regions, then PaddleOCR on each crop to read
alphanumeric bib numbers. This module only returns read results; it does not
write Supabase tags.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

# Paddle flags must be set before PaddleOCR imports Paddle internals.
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_use_onednn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_pir_api", "0")
os.environ.setdefault("FLAGS_allocator_strategy", "naive_best_fit")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "1")

import cv2
import numpy as np
from django.conf import settings
from ultralytics import YOLO

logger = logging.getLogger(__name__)
logging.getLogger("ppocr").setLevel(logging.WARNING)

BIB_CLASS_KEYWORDS = ("bib", "number")
NON_BIB_CLASS_KEYWORDS = ("runner", "person", "athlete")
DEFAULT_BIB_REGEX = r"[A-Z]{0,4}\d{2,6}[A-Z]{0,4}"

_yolo_model = None
_ocr_reader = None


@dataclass(frozen=True)
class ReaderOptions:
    yolo_conf: float = 0.05
    yolo_iou: float = 0.50
    yolo_max_det: int = 50
    yolo_imgsz: int = 1280
    ocr_conf: float = 0.50
    upscale_factor: int = 3
    crop_padding_ratio: float = 0.04
    auto_confidence: float = 0.85
    review_confidence: float = 0.50


def get_reader_options() -> ReaderOptions:
    return ReaderOptions(
        yolo_conf=float(getattr(settings, "AI_PHOTOS_YOLO_CONF", 0.05)),
        yolo_iou=float(getattr(settings, "AI_PHOTOS_YOLO_IOU", 0.50)),
        yolo_max_det=int(getattr(settings, "AI_PHOTOS_YOLO_MAX_DET", 50)),
        yolo_imgsz=int(getattr(settings, "AI_PHOTOS_YOLO_IMGSZ", 1280)),
        ocr_conf=float(getattr(settings, "AI_PHOTOS_OCR_CONF", 0.50)),
        upscale_factor=int(getattr(settings, "AI_PHOTOS_UPSCALE_FACTOR", 3)),
        crop_padding_ratio=float(getattr(settings, "AI_PHOTOS_CROP_PADDING_RATIO", 0.04)),
        auto_confidence=float(getattr(settings, "AI_PHOTOS_AUTO_CONFIDENCE", 0.85)),
        review_confidence=float(getattr(settings, "AI_PHOTOS_REVIEW_CONFIDENCE", 0.50)),
    )


def get_yolo_model():
    global _yolo_model
    if _yolo_model is None:
        model_path = getattr(settings, "AI_PHOTOS_YOLO_MODEL_PATH", "best.pt")
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"YOLO model not found: {model_path}")
        _yolo_model = YOLO(model_path)
    return _yolo_model


def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        from paddleocr import PaddleOCR

        _ocr_reader = PaddleOCR(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="en_PP-OCRv5_mobile_rec",
        )
    return _ocr_reader


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def get_class_name(model: Any, class_id: int) -> str:
    names = getattr(model, "names", None)
    if isinstance(names, dict):
        return str(names.get(class_id, class_id)).lower()
    if isinstance(names, list) and 0 <= class_id < len(names):
        return str(names[class_id]).lower()
    return str(class_id).lower()


def is_bib_detection(model: Any, box: Any) -> bool:
    class_id = int(box.cls[0]) if box.cls is not None else -1
    class_name = get_class_name(model, class_id)

    if any(word in class_name for word in NON_BIB_CLASS_KEYWORDS):
        return False

    return any(word in class_name for word in BIB_CLASS_KEYWORDS)


def padded_crop(image: np.ndarray, xyxy: Any, padding_ratio: float):
    img_h, img_w = image.shape[:2]
    x1, y1, x2, y2 = map(int, xyxy)

    box_w = x2 - x1
    box_h = y2 - y1
    if box_w <= 0 or box_h <= 0:
        return None, None

    pad_x = int(box_w * padding_ratio)
    pad_y = int(box_h * padding_ratio)

    x1 = clamp(x1 - pad_x, 0, img_w - 1)
    y1 = clamp(y1 - pad_y, 0, img_h - 1)
    x2 = clamp(x2 + pad_x, 1, img_w)
    y2 = clamp(y2 + pad_y, 1, img_h)

    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        return None, None

    return crop, (x1, y1, x2, y2)


def preprocess_for_ocr(crop: np.ndarray, upscale_factor: int) -> np.ndarray:
    upscaled = cv2.resize(
        crop,
        None,
        fx=upscale_factor,
        fy=upscale_factor,
        interpolation=cv2.INTER_CUBIC,
    )

    gray = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def ocr_box_height(points: Any) -> float:
    if points is None or len(points) == 0:
        return 0.0

    ys = [float(point[1]) for point in points]
    return max(ys) - min(ys)


def normalize_bib_text(text: str) -> str:
    text = str(text).upper()
    text = text.replace(" ", "").replace("_", "-")
    return re.sub(r"[^A-Z0-9-]", "", text)


def format_to_regex(bib_format: str | None) -> str | None:
    if not bib_format:
        return None

    pieces = []
    for char in bib_format.strip().upper():
        if char == "A":
            pieces.append("[A-Z]")
        elif char in {"9", "#", "0"}:
            pieces.append(r"\d")
        elif char == "*":
            pieces.append("[A-Z0-9]")
        else:
            pieces.append(re.escape(char))

    return "".join(pieces)


def build_bib_regex(bib_regex: str | None = None, bib_format: str | None = None) -> re.Pattern:
    pattern = bib_regex or format_to_regex(bib_format) or DEFAULT_BIB_REGEX
    return re.compile(pattern, re.IGNORECASE)


def iter_ocr_lines(ocr_result: Any):
    if not ocr_result:
        return

    for page in ocr_result:
        if isinstance(page, dict):
            texts = page.get("rec_texts") or []
            scores = page.get("rec_scores") or []
            polys = page.get("rec_polys") or page.get("dt_polys") or []
            for text, score, points in zip(texts, scores, polys):
                yield points, str(text), float(score)
            continue

        for line in page or []:
            if len(line) < 2 or len(line[1]) < 2:
                continue
            yield line[0], str(line[1][0]), float(line[1][1])


def extract_bib_number(ocr_result: Any, pattern: re.Pattern, min_conf: float):
    candidates = []
    for points, raw_text, confidence in iter_ocr_lines(ocr_result):
        if confidence < min_conf:
            continue

        normalized = normalize_bib_text(raw_text)
        if not normalized:
            continue

        matches = pattern.findall(normalized)
        if not matches:
            continue

        text_height = ocr_box_height(points)
        for match in matches:
            if isinstance(match, tuple):
                match = "".join(match)
            bib_number = normalize_bib_text(match)
            if not bib_number:
                continue
            score = (text_height * 2.0) + (len(bib_number) * 5.0) + confidence
            candidates.append(
                {
                    "score": score,
                    "bib_number": bib_number,
                    "ocr_confidence": confidence,
                    "raw_text": raw_text,
                    "normalized_text": normalized,
                }
            )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates[0]


def decode_image(image_bytes: bytes) -> np.ndarray:
    nparr = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not decode image bytes")
    return image


def status_for_confidence(confidence: float, options: ReaderOptions) -> str:
    if confidence >= options.auto_confidence:
        return "auto"
    if confidence >= options.review_confidence:
        return "review"
    return "discarded"


def read_bibs_from_image(
    image: np.ndarray,
    *,
    source_name: str | None = None,
    bib_regex: str | None = None,
    bib_format: str | None = None,
) -> dict[str, Any]:
    options = get_reader_options()
    model = get_yolo_model()
    ocr = get_ocr_reader()
    pattern = build_bib_regex(bib_regex=bib_regex, bib_format=bib_format)

    results = model.predict(
        source=image,
        conf=options.yolo_conf,
        iou=options.yolo_iou,
        max_det=options.yolo_max_det,
        imgsz=options.yolo_imgsz,
        save=False,
        verbose=False,
    )

    detections = []
    for result in results:
        for box in result.boxes:
            class_id = int(box.cls[0]) if box.cls is not None else -1
            class_name = get_class_name(model, class_id)
            yolo_confidence = float(box.conf[0]) if box.conf is not None else 0.0

            if not is_bib_detection(model, box):
                continue

            crop, crop_box = padded_crop(image, box.xyxy[0], options.crop_padding_ratio)
            if crop is None:
                continue

            processed_crop = preprocess_for_ocr(crop, options.upscale_factor)
            ocr_result = ocr.ocr(processed_crop)
            candidate = extract_bib_number(ocr_result, pattern, options.ocr_conf)

            detection = {
                "box": list(crop_box),
                "class_id": class_id,
                "class_name": class_name,
                "yolo_confidence": yolo_confidence,
                "bib_number": None,
                "ocr_confidence": 0.0,
                "raw_text": None,
                "status": "discarded",
            }
            if candidate:
                detection.update(
                    {
                        "bib_number": candidate["bib_number"],
                        "ocr_confidence": candidate["ocr_confidence"],
                        "raw_text": candidate["raw_text"],
                        "normalized_text": candidate["normalized_text"],
                        "status": status_for_confidence(candidate["ocr_confidence"], options),
                    }
                )
            detections.append(detection)

    best_detection = None
    readable = [item for item in detections if item["bib_number"]]
    if readable:
        best_detection = max(
            readable,
            key=lambda item: (item["ocr_confidence"], item["yolo_confidence"]),
        )

    height, width = image.shape[:2]
    return {
        "source_name": source_name,
        "width": width,
        "height": height,
        "bib_number": best_detection["bib_number"] if best_detection else None,
        "confidence": best_detection["ocr_confidence"] if best_detection else 0.0,
        "status": best_detection["status"] if best_detection else "discarded",
        "detections": detections,
    }


def read_bibs_from_bytes(
    image_bytes: bytes,
    *,
    source_name: str | None = None,
    bib_regex: str | None = None,
    bib_format: str | None = None,
) -> dict[str, Any]:
    image = decode_image(image_bytes)
    return read_bibs_from_image(
        image,
        source_name=source_name,
        bib_regex=bib_regex,
        bib_format=bib_format,
    )
