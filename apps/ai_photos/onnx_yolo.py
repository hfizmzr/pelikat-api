"""
ONNX Runtime YOLO bib detector — AVX-free replacement for the ultralytics
runtime (Paddle/PyTorch wheels are AVX-only; the staging CPU has none).

Loads the Ultralytics-export best.onnx (fixed square input, raw
(1, 4+nc, N) output: cx/cy/w/h boxes at input scale + class scores) and
reproduces the same conf threshold → NMS → max_det behaviour as
``YOLO.predict(source=..., conf=..., iou=..., max_det=..., imgsz=...)``.
"""

import ast

import cv2
import numpy as np
import onnxruntime as ort

LETTERBOX_PAD = (114, 114, 114)
INTRA_OP_THREADS = 16


class OnnxModelError(Exception):
    """The ONNX model is not a raw detect export this decoder understands."""


def letterbox(image: np.ndarray, size: int):
    """
    Resize keeping aspect ratio and pad to (size, size) with gray 114 —
    the same geometry ultralytics applies before ONNX inference.

    Returns (letterboxed_image, (scale, pad_left, pad_top)) so decoded
    boxes can be mapped back to original image coordinates.
    """
    img_h, img_w = image.shape[:2]
    scale = min(size / img_w, size / img_h)
    new_w, new_h = round(img_w * scale), round(img_h * scale)
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    dw, dh = size - new_w, size - new_h
    # single-arg round() already returns int (Python 3); no int() cast needed
    left, right = round((dw - 0.1) / 2), round((dw + 0.1) / 2)
    top, bottom = round((dh - 0.1) / 2), round((dh + 0.1) / 2)
    top, bottom, left, right = max(top, 0), max(bottom, 0), max(left, 0), max(right, 0)

    padded = cv2.copyMakeBorder(resized, top, bottom, left, right,
                                cv2.BORDER_CONSTANT, value=LETTERBOX_PAD)
    if padded.shape[0] != size or padded.shape[1] != size:
        padded = padded[:size, :size]
        if padded.shape[0] != size or padded.shape[1] != size:
            fixed = np.full((size, size, 3), LETTERBOX_PAD, dtype=padded.dtype)
            fixed[:padded.shape[0], :padded.shape[1]] = padded
            padded = fixed
    # Continuous padding centre — what ultralytics' scale_boxes subtracts —
    # so the reverse mapping stays sub-pixel consistent with the real padding.
    return padded, (scale, dw / 2, dh / 2)


def decode_yolo_output(
    output: np.ndarray,
    names: dict[int, str],
    scale: float,
    pad_left: float,
    pad_top: float,
    *,
    conf_threshold: float,
    iou_threshold: float,
    max_det: int,
) -> list[dict]:
    """
    Decode a raw Ultralytics detect export — (1, 4+nc, N), rows 0-3 are
    cx/cy/w/h in letterboxed input pixels, rows 4+ are per-class scores —
    into per-origin-image detections: {'xyxy', 'conf', 'class_id'}.

    Same filter chain as YOLO.predict: conf threshold, class-aware NMS,
    top max_det by confidence descending.
    """
    if output.ndim != 3 or output.shape[0] != 1:
        raise ValueError(f"expected (1, 4+nc, N) output, got shape {output.shape}")
    pred = np.asarray(output, dtype=np.float32)[0]

    if pred.shape[0] < 5 or pred.shape[0] != 4 + len(names):
        raise OnnxModelError(
            f"output has {pred.shape[0] - 4} class rows but names has "
            f"{len(names)} entries — not a raw detect export?"
        )

    cx, cy, w, h = pred[:4]
    class_scores = pred[4:]  # sigmoid confidences, already threshold-free

    class_id = class_scores.argmax(axis=0)
    conf = class_scores[class_id, np.arange(pred.shape[1])]
    keep = conf >= conf_threshold
    if not keep.any():
        return []

    cx, cy, w, h, conf, class_id = cx[keep], cy[keep], w[keep], h[keep], conf[keep], class_id[keep]
    x1 = cx - w / 2
    y1 = cy - h / 2
    x2 = cx + w / 2
    y2 = cy + h / 2

    # NMS in input-pixel space (matching what ultralytics does), then map
    # boxes back to the original image coordinates.
    indices = cv2.dnn.NMSBoxes(
        np.stack([x1, y1, x2 - x1, y2 - y1], axis=1).tolist(),
        conf.tolist(),
        conf_threshold,
        iou_threshold,
    )
    if len(indices) == 0:
        return []
    indices = np.array(indices).reshape(-1)[:max_det]

    detections = []
    for index in indices:
        detections.append({
            "xyxy": [
                float((x1[index] - pad_left) / scale),
                float((y1[index] - pad_top) / scale),
                float((x2[index] - pad_left) / scale),
                float((y2[index] - pad_top) / scale),
            ],
            "conf": float(conf[index]),
            "class_id": int(class_id[index]),
        })
    return detections


class OnnxDetector:
    """
    Minimal YOLO.predict-compatible wrapper over an Ultralytics ONNX
    export. Reads class names and image size from the export's embedded
    metadata, so no sidecar files are needed.
    """

    def __init__(self, model_path: str):
        options = ort.SessionOptions()
        options.intra_op_num_threads = INTRA_OP_THREADS
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            model_path, sess_options=options, providers=["CPUExecutionProvider"],
        )

        input_meta = self.session.get_inputs()[0]
        self.input_name = input_meta.name
        input_shape = input_meta.shape
        if len(input_shape) != 4 or not isinstance(input_shape[2], int):
            raise OnnxModelError(
                f"expected a fixed-size export input, got {input_shape} — "
                "export with yolo export format=onnx (no dynamic=True)"
            )
        self.imgsz = int(input_shape[2])

        metadata = self.session.get_modelmeta().custom_metadata_map
        try:
            self.names: dict[int, str] = {
                int(k): str(v) for k, v in ast.literal_eval(metadata["names"]).items()
            }
        except KeyError as exc:
            raise OnnxModelError("export metadata missing 'names'") from exc

    def predict(
        self,
        image: np.ndarray,
        *,
        conf: float = 0.05,
        iou: float = 0.50,
        max_det: int = 50,
    ) -> list[dict]:
        """letterbox → session.run → decode, mapped to original coords."""
        letterboxed, (scale, pad_left, pad_top) = letterbox(image, self.imgsz)
        blob = letterboxed.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        output = self.session.run(None, {self.input_name: blob})[0]
        return decode_yolo_output(
            output,
            self.names,
            scale,
            pad_left,
            pad_top,
            conf_threshold=conf,
            iou_threshold=iou,
            max_det=max_det,
        )
