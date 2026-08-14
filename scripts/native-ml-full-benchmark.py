#!/usr/bin/env python3
"""Latency benchmark for CLIP visual and CLIP textual embedding — native
mlx-swift vs. onnxruntime, side by side. Generates docs/native-ml-benchmarks.md.

WHY THIS EXISTS
---------------
The production default CLIP model (ViT-B-32__openai) stays on the
CLIPEncoder/CLIPText mlx fast path and never takes the ZooCLIP branch (see
Models.swift) — so the model most installs actually run had no latency number
anywhere. The SigLIP/SigLIP2 family does take the ZooCLIP branch and had a
native-vs-onnxruntime comparison (an earlier, since-removed sibling script),
but that comparison excluded the default model. This script drives the
`fullbench` binary mode (main.swift) that covers both in one process, one
table, regenerable by anyone instead of a one-off number in a commit message.

TEST IMAGES
-----------
Five real photos, not synthetic noise: well-known COCO val2017 images (the
"two cats" one is HuggingFace's own CLIP model card worked example; the
others are similarly famous reference images from pycocotools' and
Detectron2's own demos — see IMAGE_SOURCES below for exact URLs and
provenance notes). Timed iterations cycle through all five so the reported
median reflects varied real content instead of one image's caching quirks.
Downloaded on demand and cached locally — never committed to this repo.

MODEL COVERAGE
---------------
Defaults to the full SigLIP/SigLIP2 registry (17 models, see ALL_SIGLIP_MODELS
below — kept in sync with SigLIPRegistry.models in SigLIPNative.swift) plus
the production default (ViT-B-32__openai), since every one of them is a
native-vs-onnxruntime comparison ZooCLIP can actually make (see ZooCLIP.init:
useNative only trips for a SigLIPRegistry hit). Every other zoo model
(ViT-B-16, ViT-L-14, LAION variants, ...) has no native path, so benchmarking
it here would just compare onnxruntime against itself.

USAGE
-----
  python3 scripts/native-ml-full-benchmark.py
  python3 scripts/native-ml-full-benchmark.py --iterations 30
  python3 scripts/native-ml-full-benchmark.py --extra-clip-models none
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

# The full SigLIP/SigLIP2 registry — keep in sync with SigLIPRegistry.models
# in SigLIPNative.swift. Every entry here gets a real native-vs-onnxruntime
# comparison (see MODEL COVERAGE above); models already live in the local
# clip-dir cache from ordinary use, so running all of them costs time, not
# bandwidth.
ALL_SIGLIP_MODELS = [
    "ViT-B-16-SigLIP__webli",
    "ViT-B-16-SigLIP2__webli",
    "ViT-L-16-SigLIP2-256__webli",
    "ViT-SO400M-16-SigLIP2-384__webli",
    "ViT-SO400M-14-SigLIP2-378__webli",
    "ViT-SO400M-16-SigLIP2-512__webli",
    "ViT-L-16-SigLIP2-512__webli",
    "ViT-SO400M-16-SigLIP2-256__webli",
    "ViT-SO400M-14-SigLIP2__webli",
    "ViT-L-16-SigLIP2-384__webli",
    "ViT-B-32-SigLIP2-256__webli",
    "ViT-SO400M-14-SigLIP-384__webli",
    "ViT-L-16-SigLIP-384__webli",
    "ViT-L-16-SigLIP-256__webli",
    "ViT-B-16-SigLIP-512__webli",
    "ViT-B-16-SigLIP-384__webli",
    "ViT-B-16-SigLIP-256__webli",
]
DEFAULT_EXTRA_CLIP_MODELS = ALL_SIGLIP_MODELS

# (local filename, source URL, one-line provenance note). All five are
# well-known, permissively-licensed COCO val2017 images already hot-linked by
# other ML tooling's own docs/demos — not arbitrary picks.
IMAGE_SOURCES = {
    "clip-cats.jpg": (
        "http://images.cocodataset.org/val2017/000000039769.jpg",
        "COCO val2017 000000039769.jpg — two cats on a couch, the image used "
        "in HuggingFace's own CLIP model card example.",
    ),
    "clip-livingroom.jpg": (
        "http://images.cocodataset.org/val2017/000000000139.jpg",
        "COCO val2017 000000000139.jpg — indoor living/dining room scene, a "
        "long-standing COCO API getting-started example image.",
    ),
    "clip-bear.jpg": (
        "http://images.cocodataset.org/val2017/000000000285.jpg",
        "COCO val2017 000000000285.jpg — close-up portrait of a brown bear.",
    ),
    "clip-dogwalk.jpg": (
        "http://images.cocodataset.org/val2017/000000324158.jpg",
        "COCO val2017 000000324158.jpg — person walking a dog on a paved "
        "path, the image pycocotools' own demo notebook queries by category "
        "(person/dog/skateboard).",
    ),
    "clip-guard.jpg": (
        "http://images.cocodataset.org/val2017/000000439715.jpg",
        "COCO val2017 000000439715.jpg — mounted ceremonial guard, the input "
        "image Detectron2's own Colab quick-start demo downloads.",
    ),
}

CLIP_RE = re.compile(
    r"^BENCH clip model=(?P<model>\S+) native_visual_ms=(?P<nv>[\d.]+) onnx_visual_ms=(?P<ov>[\d.]+) "
    r"native_textual_ms=(?P<nt>[\d.]+) onnx_textual_ms=(?P<ot>[\d.]+) n=(?P<n>\d+)$"
)


def build_release(native_ml_dir: str) -> str:
    print(f"[bench] swift build -c release in {native_ml_dir} ...", flush=True)
    subprocess.run(["swift", "build", "-c", "release"], cwd=native_ml_dir, check=True)
    return os.path.join(native_ml_dir, ".build", "release", "immich-ml-native")


def ensure_metallib(binary: str, native_ml_dir: str) -> None:
    """See scripts/native-ml-preflight.py's ensure_metallib — same problem,
    same fix: swift build does not place mlx.metallib beside the binary."""
    dst = os.path.join(os.path.dirname(os.path.abspath(binary)), "mlx.metallib")
    if os.path.isfile(dst):
        return
    candidates = subprocess.run(
        ["find", "/opt/homebrew", native_ml_dir, "-name", "mlx.metallib"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    candidates = [c for c in candidates if os.path.abspath(c) != dst]
    if not candidates:
        raise RuntimeError("mlx.metallib not found (build the debug target once first)")
    print(f"[bench] copying {candidates[0]} -> {dst}", flush=True)
    shutil.copy2(candidates[0], dst)


def ensure_bench_images(cache_dir: str) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    paths = {}
    for fname, (url, note) in IMAGE_SOURCES.items():
        dest = os.path.join(cache_dir, fname)
        if not os.path.isfile(dest):
            print(f"[bench] downloading {fname} <- {url}", flush=True)
            req = urllib.request.Request(url, headers={"User-Agent": "immich-apple-silicon-benchmark"})
            with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
        paths[fname] = dest
    return paths


def run_fullbench(binary: str, clip_dir: str, images: dict,
                   iters: int, warmup: int, extra_models: list, timeout: int) -> list:
    env = {
        **os.environ,
        "ML_CLIP_DIR": clip_dir,
        "BENCH_ITERS": str(iters),
        "BENCH_WARMUP": str(warmup),
        "BENCH_CLIP_IMAGES": ",".join(images.values()),
        "BENCH_CLIP_MODELS": ",".join(extra_models),
    }
    print(f"[bench] running fullbench (iters={iters}, warmup={warmup}, "
          f"extra CLIP models={extra_models or 'none'}) ...", flush=True)
    proc = subprocess.run([binary, "fullbench"], env=env, capture_output=True, text=True, timeout=timeout)
    clip_rows = []
    for line in proc.stdout.splitlines():
        if not line.startswith("BENCH"):
            continue
        print(f"[bench]   {line}")
        if m := CLIP_RE.match(line.strip()):
            clip_rows.append({
                "model": m["model"], "native_visual_ms": float(m["nv"]), "onnx_visual_ms": float(m["ov"]),
                "native_textual_ms": float(m["nt"]), "onnx_textual_ms": float(m["ot"]), "n": int(m["n"]),
            })
        else:
            print(f"[bench] FAIL — unparsed line: {line.strip()}")
    if proc.returncode != 0 and not clip_rows:
        print(proc.stderr[-2000:])
        raise RuntimeError(f"fullbench exited {proc.returncode} with no parsable results")
    return clip_rows


def sysctl(name: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "?"


def gpu_core_count() -> str:
    try:
        out = subprocess.run(["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True,
                              timeout=15).stdout
        m = re.search(r"Total Number of Cores:\s*(\d+)", out)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def hardware_line() -> str:
    chip = sysctl("machdep.cpu.brand_string")
    perf = sysctl("hw.perflevel0.physicalcpu")
    eff = sysctl("hw.perflevel1.physicalcpu")
    gpu = gpu_core_count()
    mem_bytes = sysctl("hw.memsize")
    mem_gb = f"{int(mem_bytes) / (1024**3):.0f}GB" if mem_bytes.isdigit() else "?"
    cpu = f"{perf}P+{eff}E" if perf.isdigit() and eff.isdigit() else sysctl("hw.ncpu")
    return f"{chip} · {cpu} CPU · {gpu}-core GPU · {mem_gb} unified memory"


def git_sha(repo_dir: str) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir,
                               capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "?"


def display_model(name: str) -> str:
    base = name.split("__")[0]
    return f"{base} (default)" if "openai" in name else base


def render_markdown(clip_rows: list, repo_dir: str, iters: int, warmup: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sha = git_sha(repo_dir)

    out = [
        "# native-ml latency benchmarks",
        "",
        "Auto-generated by `scripts/native-ml-full-benchmark.py` — do not hand-edit."
        " Regenerate with:",
        "",
        "```",
        "python3 scripts/native-ml-full-benchmark.py",
        "```",
        "",
        f"Generated {now} · commit `{sha}` · median of {iters} calls, {warmup} warmup.",
        "",
        f"**Hardware:** {hardware_line()}",
        "",
        "## CLIP visual: native mlx-swift vs. onnxruntime",
        "",
        "| Model | ONNX visual | Native visual | Speedup |",
        "|---|---:|---:|---:|",
    ]
    for r in clip_rows:
        vs = r["onnx_visual_ms"] / r["native_visual_ms"]
        out.append(
            f"| {display_model(r['model'])} | {r['onnx_visual_ms']:.0f}ms | {r['native_visual_ms']:.0f}ms | "
            f"**{vs:.1f}x** |"
        )

    out += [
        "",
        "## CLIP textual: native mlx-swift vs. onnxruntime",
        "",
        "| Model | ONNX textual | Native textual | Speedup |",
        "|---|---:|---:|---:|",
    ]
    for r in clip_rows:
        ts = r["onnx_textual_ms"] / r["native_textual_ms"]
        out.append(
            f"| {display_model(r['model'])} | {r['onnx_textual_ms']:.0f}ms | {r['native_textual_ms']:.1f}ms | "
            f"**{ts:.1f}x** |"
        )

    out += [
        "",
        "## Test images",
        "",
        f"{len(IMAGE_SOURCES)} real photos from a well-known community reference set"
        " (COCO val2017), downloaded on demand and cached outside the repo (not"
        " committed here). Timed iterations cycle through all of them:",
        "",
    ]
    for fname, (url, note) in IMAGE_SOURCES.items():
        out.append(f"- **{fname}**: {note}\n  <{url}>")
    out.append("")
    return "\n".join(out)


def main() -> int:
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--native-ml-dir", default=os.path.join(repo_dir, "native-ml"))
    ap.add_argument("--binary", help="pre-built binary path; skips swift build -c release")
    ap.add_argument("--clip-dir", default=os.path.expanduser("~/.cache/immich-ml-native/clip"))
    ap.add_argument("--image-cache-dir", default=os.path.expanduser("~/.cache/immich-ml-native/bench-images"))
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=5400,
                     help="subprocess timeout; the full SigLIP registry default is slow to "
                          "run end to end, and a model missing from the clip-dir cache needs "
                          "a one-time multi-GB external-data download")
    ap.add_argument("--extra-clip-models", default=",".join(DEFAULT_EXTRA_CLIP_MODELS),
                     help="comma-separated extra zoo CLIP models to benchmark alongside "
                          "the production default — defaults to the full SigLIP/SigLIP2 "
                          "registry (all 17), or 'none'")
    ap.add_argument("--out", default=os.path.join(repo_dir, "docs", "native-ml-benchmarks.md"))
    args = ap.parse_args()

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        print("[bench] FAIL — this benchmark must run on Apple Silicon (mlx/Metal)")
        return 1

    extra_models = [] if args.extra_clip_models.strip().lower() == "none" else [
        m.strip() for m in args.extra_clip_models.split(",") if m.strip()
    ]

    binary = args.binary or build_release(args.native_ml_dir)
    if not os.path.isfile(binary):
        print(f"[bench] FAIL — binary not found: {binary}")
        return 1
    if not os.path.isdir(args.clip_dir):
        print(f"[bench] FAIL — clip dir not found: {args.clip_dir} (run native-ml/scripts/fetch_clip_model.sh)")
        return 1
    ensure_metallib(binary, args.native_ml_dir)
    images = ensure_bench_images(args.image_cache_dir)

    try:
        clip_rows = run_fullbench(binary, args.clip_dir, images,
                                   args.iterations, args.warmup, extra_models, args.timeout)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"[bench] FAIL — {e}")
        return 1

    if not clip_rows:
        print("[bench] FAIL — no CLIP results")
        return 1

    md = render_markdown(clip_rows, repo_dir, args.iterations, args.warmup)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(md + "\n")
    print(f"\n[bench] wrote {args.out}")
    print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
