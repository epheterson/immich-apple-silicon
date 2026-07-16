#!/usr/bin/env python3
"""Face-align reference: detect (Vision) -> landmarks -> norm_crop -> embed.
Saves landmarks, the Python-aligned RGB crop, and the full-pipeline embedding
so the Swift align can be compared pixelwise + end-to-end. Run in ml src cwd."""

import sys, json

sys.path.insert(0, "/opt/homebrew/Cellar/immich-accelerator/1.5.32/libexec/ml")
import numpy as np
import cv2
from src.models.face_detect import detect_faces
from insightface.utils import face_align

IMG = "/tmp/face_test.jpg"
data = open(IMG, "rb").read()
faces, W, H = detect_faces(data)
print(f"faces={len(faces)} img={W}x{H}")
face = next((f for f in faces if "landmarks" in f), None)
if face is None:
    print("NO face with landmarks; boxes:", [f.get("boundingBox") for f in faces])
    sys.exit(1)

lmk = np.array(face["landmarks"], dtype=np.float32)
print("landmarks:", [[round(v, 2) for v in p] for p in lmk.tolist()])
json.dump(lmk.tolist(), open("/tmp/face_landmarks.json", "w"))

img_bgr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
aligned_bgr = face_align.norm_crop(img_bgr, lmk, image_size=112)
aligned_rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB)
aligned_rgb.tofile("/tmp/face_aligned_py.rgb")  # 112*112*3 uint8, RGB

from src.models.face_embed import get_recognition_model

model = get_recognition_model("buffalo_l")
emb = model.get_feat(aligned_bgr).flatten().astype(np.float32)
emb = emb / np.linalg.norm(emb)
np.save("/tmp/face_emb_py.npy", emb)
print("E_py first5:", [round(float(v), 5) for v in emb[:5]])
print("saved /tmp/face_landmarks.json, /tmp/face_aligned_py.rgb, /tmp/face_emb_py.npy")
