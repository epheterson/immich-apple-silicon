# native-ml — Swift ML service (work in progress)

A single native Swift binary that replaces the ~1.5 GB Python ML venv (`ml/`
fork: FastAPI + mlx + onnxruntime + torch + insightface + opencv). Goal: a
byte-for-byte drop-in for Immich's `/predict` contract that eliminates the whole
pip/torch/mlx-pin install-fragility class (#17, #38, #103, #105).

**Plan:** `docs/plans/2026-07-16-native-swift-ml-service.md`.

## Status (2026-07-16): every ML algorithm proven native with parity

Validated on Apple Silicon (Mac Mini M4) against the running Python service:

| Component | Native path | Parity vs Python |
| --- | --- | --- |
| CLIP visual (ViT-B-32) | mlx-swift, same safetensors | cosine **1.0** |
| CLIP text + BPE tokenizer | mlx-swift + native tokenizer | cosine **1.0**, ids byte-exact |
| Face detect + landmarks | Vision `VNDetectFaceLandmarksRequest` | same engine |
| Face align (`norm_crop`) | similarity/Umeyama + bilinear warp | cosine **1.0** (warp-only) |
| Face embed (ArcFace `w600k_r50`) | onnxruntime C ABI, same `.onnx` | cosine **1.0** |
| Full face pipeline | ImageIO decode -> align -> embed | **0.9997** vs Python (JPEG-decoder-bound, harmless) |
| OCR | Vision `VNRecognizeTextRequest` | same engine |
| HTTP `/predict` | Network.framework | cosine 1.0 e2e (visual) |

Same weights + same models => embeddings stay compatible with Immich's existing
search index and face clusters. **No re-index, no re-cluster.**

## What's here

Validated compute seed. `Sources/immich-ml-native/`:
- `CLIP.swift` — ViT-B-32 visual encoder
- `CLIPText.swift`, `CLIPTokenizer.swift` — text encoder + CLIP byte-level BPE
- `FaceAlign.swift` — ArcFace `norm_crop` (similarity transform + warp)
- `Vision.swift` — OCR + face detect via Vision
- `Server.swift` — Network.framework HTTP (prototype)
- `main.swift` — parity harnesses (`texttest`, `aligntest`, `serve`) + self-test

`scripts/` — the Python/C++ parity harness used to generate references and prove
cosine parity on-device.

## Remaining (per the plan, no research unknowns left)

ORTSession (onnxruntime C-ABI Swift wrapper), in-process face pipeline, full
`/predict` dispatch + model registry, relocatable bundle (colocate `mlx.metallib`
+ `libonnxruntime`), integration behind an `ML_ENGINE=native` flag with the venv
retained as fallback, and an on-prod soak diffing search + face clusters.

## Build (on Apple Silicon)

```
brew install onnxruntime          # face embed (C ABI)
cd native-ml && swift build
# mlx-swift does not emit its metallib for a bare CLI build; colocate it once:
cp "$(python3 -c 'import mlx,os;print(os.path.dirname(mlx.__file__))')/lib/mlx.metallib" .build/debug/
```
