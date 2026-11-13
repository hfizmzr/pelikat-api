"""
Bib reader for AI photo tagging.

Runs the YOLO ONNX export to locate bib regions, then RapidOCR
(PP-OCRv5 mobile ONNX) on each crop to read alphanumeric bib numbers.
This module only returns read results; it does not write Supabase tags.
"""

import logging
import os
import re
from dataclasses import dataclass

import cv2
import numpy as np
from django.conf import settings

from .onnx_yolo import OnnxDetector

BIB_CLASS_KEYWORDS = ("bib", "number")
NON_BIB_CLASS_KEYWORDS = ("runner", "person", "athlete")
DEFAULT_BIB_REGEX = r"[A-Z]{0,4}\d{2,6}[A-Z]{0,4}"

# RapidOCR's global image cap (px per side) — the old Paddle path had no
# cap, so keep a generous bound instead of the 2000 default.
OCR_MAX_SIDE_LEN = 6000

# Text-detector settings PaddleOCR 3.x applies to PP-OCRv5_mobile_det (its
# OCR pipeline config + the model's inference.yml). RapidOCR's own defaults
# differ on every one of these (limit_side_len 736, box_thresh 0.5,
# unclip 1.6, dilation on, 0.5/0.5 normalisation); with them the detector
# fragments bib numbers ("90048" -> "004", "3297" -> "32") and reads the
# "10KM" badge instead. Measured on 229 hand-labelled bib crops:
# RapidOCR defaults 67 correct reads, these settings 203 (Paddle: 198).
PADDLE_DET_PARAMS = {
    "Det.limit_side_len": 64,
    "Det.limit_type": "min",
    "Det.thresh": 0.3,
    "Det.box_thresh": 0.6,
    "Det.unclip_ratio": 1.5,
    "Det.use_dilation": False,
    "Det.mean": [0.485, 0.456, 0.406],
    "Det.std": [0.229, 0.224, 0.225],
    # Paddle keeps every recognised line (callers apply their own conf
    # floor) and has no small-image padding / aspect-ratio shortcut.
    "Global.text_score": 0.0,
    "Global.min_height": -1,
    "Global.width_height_ratio": -1,
}
ORT_THREADS = int(os.environ.get("AI_PHOTOS_ORT_THREADS", "2"))

logger = logging.getLogger(__name__)
logging.getLogger("rapidocr").setLevel(logging.WARNING)

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
        model_path = getattr(settings, "AI_PHOTOS_YOLO_MODEL_PATH", "models/best.onnx")
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"YOLO ONNX model not found: {model_path}")
        _yolo_model = OnnxDetector(model_path)
    return _yolo_model


class RapidOcrEngine:
    """
    PaddleOCR-compatible wrapper over RapidOCR (PP-OCRv5 mobile det+rec
    ONNX models, bundled in models/rapidocr/ — no runtime downloads).

    ``ocr(image)`` returns one page dict shaped like PaddleOCR 3.x output
    (``rec_texts`` / ``rec_scores`` / ``rec_polys``), which both
    iter_ocr_lines() (bib crops) and apps/documents _iter_ocr_lines()
    consume unchanged.
    """

    def __init__(
        self,
        det_model_path: str,
        rec_model_path: str,
        rec_keys_path: str,
        intra_op_threads: int = ORT_THREADS,
    ):
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import LangRec, OCRVersion

        self.engine = RapidOCR(params={
            # The old pipeline disabled textline orientation; cls adds a
            # third session we don't need for printed bib numbers.
            "Global.use_cls": False,
            "Global.max_side_len": OCR_MAX_SIDE_LEN,
            **PADDLE_DET_PARAMS,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_path": str(det_model_path),
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.lang_type": LangRec.EN,
            "Rec.model_path": str(rec_model_path),
            "Rec.rec_keys_path": str(rec_keys_path),
            "EngineConfig.onnxruntime.intra_op_num_threads": intra_op_threads,
        })

    def ocr(self, image: np.ndarray):
        result = self.engine(image)
        if result is None:
            return []
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        return [{
            "rec_texts": list(result.txts or []),
            "rec_scores": [float(score) for score in result.scores or []],
            "rec_polys": [np.asarray(box) for box in boxes],
        }]


def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        _ocr_reader = RapidOcrEngine(
            settings.AI_PHOTOS_OCR_DET_MODEL,
            settings.AI_PHOTOS_OCR_REC_MODEL,
            settings.AI_PHOTOS_OCR_REC_KEYS,
        )
    return _ocr_reader


def warm_up_models():
    """
    Load both models and run each once. The first real inference otherwise
    pays ONNX session start-up, which the first photo of a batch would feel.
    """
    options = get_reader_options()
    model = get_yolo_model()
    model.predict(
        np.zeros((model.imgsz, model.imgsz, 3), dtype=np.uint8),
        conf=options.yolo_conf,
        iou=options.yolo_iou,
        max_det=options.yolo_max_det,
    )

    # OCR warm run on a crop containing digits so det *and* rec both load.
    blank = np.full((64, 160, 3), 255, dtype=np.uint8)
    cv2.putText(blank, "00", (8, 52), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (0, 0, 0), 5)
    get_ocr_reader().ocr(blank)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def get_class_name(model: OnnxDetector, class_id: int) -> str:
    names = getattr(model, "names", None)
    if isinstance(names, dict):
        return str(names.get(class_id, class_id)).lower()
    if isinstance(names, list) and 0 <= class_id < len(names):
        return str(names[class_id]).lower()
    return str(class_id).lower()


def is_bib_detection(model: OnnxDetector, class_id: int) -> bool:
    class_name = get_class_name(model, class_id)

    if any(word in class_name for word in NON_BIB_CLASS_KEYWORDS):
        return False

    return any(word in class_name for word in BIB_CLASS_KEYWORDS)


def padded_crop(image: np.ndarray, xyxy, padding_ratio: float):
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
    """x3 cubic upscale + histogram equalisation (same as the Paddle path).
    With Paddle-matched detector settings this is the best-scoring input on
    the labelled crop set (203 correct vs 183 on the raw crop)."""
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


def ocr_box_height(points) -> float:
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


def iter_ocr_lines(ocr_result):
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


def extract_bib_number(ocr_result, pattern: re.Pattern, min_conf: float):
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
) -> dict[str, object]:
    options = get_reader_options()
    model = get_yolo_model()
    ocr = get_ocr_reader()
    pattern = build_bib_regex(bib_regex=bib_regex, bib_format=bib_format)

    detections = []
    for detected in model.predict(
        image,
        conf=options.yolo_conf,
        iou=options.yolo_iou,
        max_det=options.yolo_max_det,
    ):
        class_id = detected["class_id"]
        class_name = get_class_name(model, class_id)
        yolo_confidence = float(detected["conf"])

        if not is_bib_detection(model, class_id):
            continue

        crop, crop_box = padded_crop(image, detected["xyxy"], options.crop_padding_ratio)
        if crop is None:
            continue

        ocr_result = ocr.ocr(preprocess_for_ocr(crop, options.upscale_factor))
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
                    "extract_score": candidate["score"],
                    "status": status_for_confidence(candidate["ocr_confidence"], options),
                }
            )
        else:
            detection["extract_score"] = 0.0
        detections.append(detection)

    # Ranking decided on staging parity data (manager-approved): select by
    # extract score (height + length + OCR-confidence composite from
    # extract_bib_number), then OCR confidence, then YOLO confidence.
    # Rationale: with RapidOCR, both bib4 reads saturate at OCR conf 1.0,
    # so (ocr_conf, yolo_conf) alone flips the old outcome to the shorter
    # "3004" misread; the extract score restores the deterministic old
    # outcome (longer full-number "5073" wins). Single-read samples
    # (bib5 "499") and no-read samples (bib2) are unaffected.
    best_detection = None
    readable = [item for item in detections if item["bib_number"]]
    if readable:
        best_detection = max(
            readable,
            key=lambda item: (
                item["extract_score"],
                item["ocr_confidence"],
                item["yolo_confidence"],
            ),
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
) -> dict:
    image = decode_image(image_bytes)
    return read_bibs_from_image(
        image,
        source_name=source_name,
        bib_regex=bib_regex,
        bib_format=bib_format,
    )
