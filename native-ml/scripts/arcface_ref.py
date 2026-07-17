#!/usr/bin/env python3
"""Inspect InsightFace ArcFace (w600k_r50.onnx) + deterministic reference embedding,
and check native-conversion tooling. Run with the prod ml venv python."""

import glob, os, sys
import numpy as np

model = os.path.expanduser("~/.insightface/models/buffalo_l/w600k_r50.onnx")
print("model exists:", os.path.exists(model), model)

import onnxruntime as ort

so = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
inp = so.get_inputs()[0]
out = so.get_outputs()[0]
print("INPUT :", inp.name, inp.shape, inp.type)
print("OUTPUT:", out.name, out.shape, out.type)

# deterministic 112x112x3 input (so Swift/CoreML can reproduce the exact tensor)
n = int(inp.shape[2]) if isinstance(inp.shape[2], int) else 112
x = np.zeros((1, 3, n, n), dtype=np.float32)
for c in range(3):
    for i in range(n):
        for j in range(n):
            # arcface preprocessing is (pixel-127.5)/128; emulate a fixed "image"
            px = (i * 173 + j * 13 + c * 71) % 256
            x[0, c, i, j] = (px - 127.5) / 128.0
emb = so.run([out.name], {inp.name: x})[0][0].astype(np.float32)
print("EMB dim=", emb.shape[0], "L2=%.4f" % float(np.linalg.norm(emb)))
print("first6:", [round(float(v), 5) for v in emb[:6]])
np.save("/tmp/arcface_ref_emb.npy", emb)
x.tofile("/tmp/arcface_input.f32")  # raw [1,3,112,112] float32 for Swift to load
print("saved /tmp/arcface_ref_emb.npy + /tmp/arcface_input.f32 (n=%d)" % n)

# conversion tooling check
for mod in ("coremltools", "onnx"):
    try:
        m = __import__(mod)
        print(f"HAVE {mod} {getattr(m,'__version__','?')}")
    except Exception as e:
        print(f"NO {mod}: {e}")
