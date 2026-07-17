#!/usr/bin/env python3
"""CLIP text-encoder reference + tokenizer oracle + text-model weight keys.
Run with the accelerator ml venv on the Mini."""

import json, os, glob
import numpy as np

PHRASES = [
    "a photo of a cat",
    "a dog running on the beach",
    "sunset over the mountains",
    "immich native swift",
    "OCR Test 123",
]

from mlx_clip import mlx_clip

repo = "mlx-community/clip-vit-base-patch32"
m = mlx_clip(repo)
print("mlx_clip attrs:", [a for a in dir(m) if not a.startswith("__")])

# --- text embeddings (the parity target) ---
embs = {}
for p in PHRASES:
    e = np.array(m.text_encoder(p)).flatten().astype(np.float32)
    e = e / np.linalg.norm(e)
    embs[p] = e
    print(f"TEXT {p!r}: dim={e.shape[0]} first5={[round(float(v),5) for v in e[:5]]}")
np.savez("/tmp/clip_text_ref.npz", **{str(i): embs[p] for i, p in enumerate(PHRASES)})
json.dump(PHRASES, open("/tmp/clip_text_phrases.json", "w"))

# --- tokenizer oracle: dump token ids mlx_clip produces for each phrase ---
tok = None
for name in ("tokenizer", "_tokenizer", "processor", "text_processor"):
    if hasattr(m, name):
        tok = getattr(m, name)
        print("tokenizer attr:", name, type(tok))
        break
if tok is not None:
    print("tok attrs:", [a for a in dir(tok) if not a.startswith("_")][:40])
    for p in PHRASES:
        for meth in ("tokenize", "encode", "__call__"):
            try:
                out = getattr(tok, meth)(p)
                print(f"IDS[{meth}] {p!r}: {np.array(out).flatten().tolist()[:80]}")
                break
            except Exception as ex:
                pass

# --- locate the model + tokenizer files on disk ---
from huggingface_hub import snapshot_download

try:
    d = snapshot_download(repo)
    print("model dir:", d)
    print("files:", sorted(os.listdir(d)))
except Exception as ex:
    print("snapshot lookup failed:", ex)

# --- text-model weight keys from the safetensors ---
import mlx.core as mx

for st in glob.glob(os.path.join(d, "*.safetensors")):
    w = mx.load(st)
    tkeys = sorted(k for k in w if "text" in k.lower())
    print(f"\n=== {os.path.basename(st)}: {len(tkeys)} text keys ===")
    for k in tkeys[:60]:
        print(f"  {k}  {tuple(w[k].shape)}")
    break
