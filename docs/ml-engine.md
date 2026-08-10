# ML service

The ML service runs Immich's CLIP, face, and OCR inference natively on Apple Silicon.

| Task | Hardware | Framework |
|------|----------|-----------|
| CLIP embeddings (image + text) | GPU (Metal) | mlx-swift |
| Face detection + landmarks | Neural Engine | Apple Vision |
| Face recognition | CPU | InsightFace ArcFace (onnxruntime) |
| OCR | Neural Engine | Apple Vision |

As of 1.6.0 this runs as a **native Swift engine**: a single binary with the models and libraries bundled, no Python. It replaces the ~1.5 GB Python venv (torch, mlx, onnxruntime, opencv, insightface) and the dependency-pin fragility that came with it. It uses the same weights and models as the Python service, so embeddings stay in the same space as an existing Immich search index and face clusters (no re-index, no re-cluster).

As of 1.7.0 the native engine supports the **full CLIP model zoo**: whatever model you select in Immich (ViT-B-16, ViT-L-14, LAION variants, the SigLIP family, ...) is downloaded from Immich's own model repository on first use and run natively through onnxruntime with Immich's exact preprocessing and tokenization, so results match the Docker ML service. The default ViT-B-32 uses an even faster mlx path.

The full SigLIP and SigLIP2 family (17 model/resolution combinations, e.g. `ViT-SO400M-16-SigLIP2-384__webli`) also runs this faster mlx path instead of onnxruntime — 2x-7x faster on CLIP image embeddings depending on model size (bigger models see a bigger win; a fixed CPU-side resize cost dominates more of the total for smaller ones), measured with `scripts/native-ml-full-benchmark.py` (table: [native-ml-benchmarks.md](native-ml-benchmarks.md)). Every other zoo model still runs through onnxruntime as described above.

Changing the model in Immich takes effect on the next Smart Search job, not immediately: the new model is fetched the first time Immich asks for it, which for the largest models is several GB. While that download runs, the menu bar shows "Downloading model…" with progress, and Immich's search jobs fail and retry until it finishes (nothing is lost, they succeed once the model is ready). You can also follow it with `immich-accelerator logs ml`.

Note that `ml-test` always probes with `ViT-B-32__openai`, so its output does not tell you which model your library is using; it prints your configured model separately.

The native engine is the default and is health-checked at startup. If its bundle or models are missing, or it fails to start, the accelerator automatically falls back to the Python service so ML is never left down. On a brand-new install the models (~740MB) are downloaded once in the background on first native start, so ML runs on the Python engine for a few minutes until they arrive, then switches to native automatically.

**Switching back to the Python engine.** If you want to force the Python service (for example to compare results, or if native misbehaves), set `ml_engine` in `~/.immich-accelerator/config.json`:

```json
{ "ml_engine": "python" }
```

Then restart the accelerator (`brew services restart epheterson/immich-accelerator/immich-accelerator`, or `immich-accelerator stop` then `start`). Set it back to `"native"` (or remove the key) and restart to return to native. Confirm which engine is live with `immich-accelerator ml-test` and check `ml.log`.

The Python engine is a managed fork of [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) by [@sebastianfredette](https://github.com/sebastianfredette), included as a git submodule; upstream changes are reviewed before merging, and contributions are made via [upstream PRs](https://github.com/sebastianfredette/immich-ml-metal/pulls).
