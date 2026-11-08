# Staging branch differences vs master

`staging` is the deployment branch for the constrained staging VPS
(`csnet-hazim`, 64-core / 16 GB RAM) whose CPU has **zero AVX**. `master`
is kept as the full-runtime baseline; `staging` swaps only the inference
runtime.

## Why staging exists

- `master` runtime = ultralytics (torch) + paddlepaddle/paddleocr (Paddle
  runtime). It is **AVX-only**: `paddlepaddle==3.0.0` ships AVX kernels and
  Paddle discontinued noavx builds for Python 3.x. On the zero-AVX VPS the
  OCR worker **crash-looped with SIGILL (ExitCode 132)**.
- The heavy torch/paddle runtime also exceeds the staging VPS memory and
  thread budgets (hence the caps below).

## Runtime diff: master vs staging

| | master | staging |
|---|---|---|
| Bib detection | ultralytics `best.pt` (torch) | `onnxruntime==1.20.1` + committed `models/best.onnx` (Ultralytics export @ imgsz 1280, raw `(1, 4+nc, 33600)` output, class names in export metadata) |
| OCR | paddlepaddle/paddleocr (PP-OCRv5) | `rapidocr==3.3.1` + committed `models/rapidocr/` (PP-OCRv5 mobile det + latin rec + latin dict) |
| Model weights | YOLO + PP-OCRv5 mobile | **same** (viva architecture unchanged) — only the inference runtime differs |
| Model source | PaddleOCR auto-downloads from Paddle's model zoo on first use | all models committed, SHA-256-verified against RapidOCR's manifest; **zero runtime downloads** |
| Requirements | paddlepaddle, paddleocr, ultralytics (+ torch chain) | onnxruntime + rapidocr only; image ~**1.9 GB smaller** |

`onnxruntime` is pinned to 1.20.1 (1.21.0+ has an ARM SIGILL regression per
upstream reports); `rapidocr` is pinned to 3.3.1 (validated PP-OCRv5
behaviour; 3.10 bundles PP-OCRv6 and changes pre/post-processing).

## Resource and thread caps (`docker-compose.staging.yml`)

- `deploy.resources.limits`: **memory 2g / cpus '2'** on both `api` and
  `photo-worker`.
- `OMP_NUM_THREADS=2`, `OPENBLAS_NUM_THREADS=2`, `MKL_NUM_THREADS=2` on both
  services (same caps also in `Dockerfile.prod`/`Dockerfile.dev`).
- ONNX Runtime `intra_op` thread count is capped in code
  (`apps/ai_photos/onnx_yolo.py`, `apps/ai_photos/reader.py`).

## Parity and test metrics (staging branch, SHIP-verified)

- **Tests**: 445 pytest passed, coverage **93.13%** (master baseline: 432
  passed / 92.77%).
- **Readable-bib parity** (same 6 bib samples, deterministic): bib4 → `5073`,
  bib5 → `499` (confidence parity within ±0.05); bib2 boxes **exact parity**.
- bib.jpg / bib3 now **decode** via the OpenCV 5 wheel's AVIF support
  (behaviour change, flagged for review).
- bib6 is detected but still discarded by the review-confidence flow
  (flagged; ruff-level risks noted for future work).

## Deployment

- Portainer builds/pulls the api image from pushes to the **`staging` branch**
  (CI builds `docker.io/hfizmzr/pelikat-api:staging` and fires
  `PORTAINER_WEBHOOK_URL_API` on both `master` and `staging` pushes).
- `master` remains the full-runtime baseline; do not merge `staging` runtime
  changes into `master`.
