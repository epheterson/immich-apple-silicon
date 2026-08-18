# native-ml — Swift ML service (work in progress)

A single native Swift binary that replaces the ~1.5 GB Python ML venv (`ml/`
fork: FastAPI + mlx + onnxruntime + torch + insightface + opencv). Goal: a
byte-for-byte drop-in for Immich's `/predict` contract that eliminates the whole
pip/torch/mlx-pin install-fragility class (#17, #38, #103, #105).

**Plan:** `docs/plans/2026-07-16-native-swift-ml-service.md`.

## Status (2026-07-16): full service running, live-parity tested

A complete native `/predict` service is built and runs on Apple Silicon (Mac
Mini M4). Every task type was driven with identical payloads against the running
Python service (`:3003`) and compared:

| Task | Native path | Parity vs Python (same request) |
| --- | --- | --- |
| clip textual | mlx-swift + native BPE tokenizer | cosine **1.0000000** (ids byte-exact) |
| clip visual (ViT-B-32) | mlx-swift, same safetensors | cosine **0.9993** (JPEG); **1.0** on matched input |
| facial-recognition | Vision landmarks -> `norm_crop` -> onnxruntime ArcFace | embed **0.9997**, bbox + score **identical** |
| ocr | Vision `VNRecognizeTextRequest` | text + boxes match |
| combined (clip+faces+ocr, one request) | concurrent dispatch | 200, all keys present |

`/`, `/ping`, `/health` all match the Python contract. Same weights + same
models => embeddings live in the same space as Immich's existing index and face
clusters. The sub-1.0 numbers are decode/resize-filter differences (ImageIO/PIL,
CGContext/PIL-bicubic), far inside search + clustering tolerance — no re-index,
no re-cluster.

## What's here

`Sources/immich-ml-native/`:
- `CLIP.swift`, `Resize.swift` — ViT-B-32 visual encoder + PIL-compatible bicubic resize
- `CLIPText.swift`, `CLIPTokenizer.swift` — text encoder + CLIP byte-level BPE
- `FaceDetect.swift`, `FaceAlign.swift`, `FaceEmbed.swift` — Vision landmarks, `norm_crop`, ArcFace embed
- `ORTSession.swift` + `Sources/COnnxShim/` — onnxruntime C-ABI wrapper
- `OCR.swift`, `Vision.swift` — Vision OCR + detect helpers
- `Predict.swift`, `Server.swift`, `Models.swift` — dispatch, HTTP, model holder
- `main.swift` — `serve` + parity harnesses (`texttest`, `aligntest`, `facetest`)

`scripts/` — the Python/C++ parity harness used to generate references on-device.

## Remaining (no research unknowns left)

Relocatable bundle (colocate `mlx.metallib` + `libonnxruntime` with `@loader_path`),
integration behind an `ML_ENGINE=native` flag with the venv retained as fallback,
an on-prod soak diffing search + face clusters, then hardening (shared CLIP
weights to avoid a double load, model registry for other CLIP archs, C-shim
error-path polish).

## Benchmarks

CLIP visual/textual latency, native mlx-swift vs. onnxruntime, measured on a
real photo: [docs/native-ml-benchmarks.md](../docs/native-ml-benchmarks.md). Regenerate
with `python3 scripts/native-ml-full-benchmark.py` (from repo root).

## Build (on Apple Silicon)

```
brew install onnxruntime          # face embed (C ABI)
cd native-ml && swift build
# mlx-swift does not emit its metallib for a bare CLI build; colocate it once:
cp "$(python3 -c 'import mlx,os;print(os.path.dirname(mlx.__file__))')/lib/mlx.metallib" .build/debug/
```
