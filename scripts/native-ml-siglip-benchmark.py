#!/usr/bin/env python3
"""Native mlx-swift vs. onnxruntime latency benchmark for the SigLIP/SigLIP2
registry (native-ml/Sources/immich-ml-native/SigLIPNative.swift).

WHY THIS EXISTS
---------------
native-mlx-siglip2 moved 17 SigLIP/SigLIP2 models off the generic onnxruntime
zoo path (ZooCLIP.swift) onto a hand-written native mlx-swift path
(SigLIPNative.swift), for speed. The commit that started this
(56e4ac0, "~22x faster than CPU ONNX") and a CHANGELOG entry for an earlier,
unrelated onnxruntime thread-count fix both quote before/after latency
numbers, but neither left behind a script — the numbers were one-off. This is
that script, generalized to the whole registry.

METHODOLOGY
-----------
Runs the real release binary's `benchtest` CLI mode (main.swift) twice per
model: once normally (native mlx-swift) and once with ZOOCLIP_FORCE_ONNX=1 (a
benchmark-only escape hatch in ZooCLIP.swift's init that makes a registry
model take the onnxruntime branch anyway). Both runs go through the exact
same Swift harness — same image load, same ZooCLIP.embedVisual/embedTextual
call, same process — so the only variable is which inference backend runs;
this isn't comparing a Python client against a Swift server; both numbers
come from identical Swift-side code. Each measurement is a warmup (discarded)
followed by N timed calls with DispatchTime.now(), median reported (see
`timeMs`/`median` in main.swift's benchtest block).

Forcing ONNX on a large model triggers ZooCLIP's normal external-data fetch
(multi-GB, skipped for native models — see the comment at ZooCLIP.swift's
`.external-data-checked` marker) on first run; expect that to take a while
for SO400M-scale models the first time.

USAGE
-----
  python3 scripts/native-ml-siglip-benchmark.py
  python3 scripts/native-ml-siglip-benchmark.py --models all
  python3 scripts/native-ml-siglip-benchmark.py --iterations 20 --markdown
"""

import argparse
import json
import os
import re
import subprocess
import sys

# One per tower scale, so the default run is fast but still representative:
# smallest (self-contained ONNX, no external data), a mid-size tower, and the
# flagship SO400M/384 (the model 56e4ac0's "~22x" figure was originally about
# — needs external-data download the first time this runs with --models all
# or explicitly including it).
DEFAULT_MODELS = [
    "ViT-B-16-SigLIP-256__webli",
    "ViT-L-16-SigLIP2-256__webli",
    "ViT-SO400M-16-SigLIP2-384__webli",
]

# Keep in sync with SigLIPRegistry.models in SigLIPNative.swift.
ALL_MODELS = [
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

BENCH_RE = re.compile(
    r"^BENCH (?P<name>\S+) visual_ms=(?P<visual>[\d.]+) textual_ms=(?P<textual>[\d.]+) n=(?P<n>\d+)$"
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
    import shutil
    shutil.copy2(candidates[0], dst)


def run_bench(binary: str, models: list, clip_dir: str, iters: int, warmup: int,
              force_onnx: bool, timeout: int) -> dict:
    env = {
        **os.environ,
        "ML_CLIP_DIR": clip_dir,
        "BENCH_MODELS": ",".join(models),
        "BENCH_ITERS": str(iters),
        "BENCH_WARMUP": str(warmup),
    }
    if force_onnx:
        env["ZOOCLIP_FORCE_ONNX"] = "1"
    label = "onnxruntime" if force_onnx else "native mlx-swift"
    print(f"[bench] running {len(models)} model(s) via {label} ...", flush=True)
    proc = subprocess.run([binary, "benchtest"], env=env, capture_output=True, text=True, timeout=timeout)
    results = {}
    for line in proc.stdout.splitlines():
        print(f"[bench]   {line}")
        m = BENCH_RE.match(line.strip())
        if m:
            results[m["name"]] = {"visual_ms": float(m["visual"]), "textual_ms": float(m["textual"])}
        elif line.strip().startswith("BENCH") and "FAILED" in line:
            name = line.split()[1]
            print(f"[bench] FAIL — {name} errored under {label}: {line.strip()}")
    missing = set(models) - set(results)
    if missing:
        print(f"[bench] FAIL — no result for {sorted(missing)} under {label}")
        if proc.stderr.strip():
            print(f"[bench]   stderr tail: {proc.stderr.strip()[-1000:]}")
        raise RuntimeError(f"missing benchmark results: {sorted(missing)}")
    return results


def render_markdown(rows: list) -> str:
    out = [
        "| Model | Native visual | ONNX visual | Speedup | Native textual | ONNX textual | Speedup |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| `{r['model']}` | {r['native_visual_ms']:.0f}ms | {r['onnx_visual_ms']:.0f}ms | "
            f"**{r['visual_speedup']:.1f}x** | {r['native_textual_ms']:.0f}ms | {r['onnx_textual_ms']:.0f}ms | "
            f"**{r['textual_speedup']:.1f}x** |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--native-ml-dir",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "native-ml"),
    )
    ap.add_argument("--binary", help="pre-built binary path; skips swift build -c release")
    ap.add_argument("--clip-dir", default=os.path.expanduser("~/.cache/immich-ml-native/clip"))
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                     help="comma-separated model names, or 'all' for the full registry")
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=1800,
                     help="per-run subprocess timeout; the onnxruntime run may need to "
                          "download multi-GB external-data weights on first use")
    ap.add_argument("--markdown", action="store_true", help="also print a markdown table")
    ap.add_argument("--json-out", help="write full results as JSON to this path")
    args = ap.parse_args()

    models = ALL_MODELS if args.models == "all" else [m.strip() for m in args.models.split(",") if m.strip()]

    binary = args.binary or build_release(args.native_ml_dir)
    if not os.path.isfile(binary):
        print(f"[bench] FAIL — binary not found: {binary}")
        return 1
    if not os.path.isdir(args.clip_dir):
        print(f"[bench] FAIL — clip dir not found: {args.clip_dir}")
        return 1
    ensure_metallib(binary, args.native_ml_dir)

    try:
        native = run_bench(binary, models, args.clip_dir, args.iterations, args.warmup,
                            force_onnx=False, timeout=args.timeout)
        onnx = run_bench(binary, models, args.clip_dir, args.iterations, args.warmup,
                          force_onnx=True, timeout=args.timeout)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"[bench] FAIL — {e}")
        return 1

    rows = []
    for name in models:
        n, o = native[name], onnx[name]
        rows.append({
            "model": name,
            "native_visual_ms": n["visual_ms"], "onnx_visual_ms": o["visual_ms"],
            "visual_speedup": o["visual_ms"] / n["visual_ms"],
            "native_textual_ms": n["textual_ms"], "onnx_textual_ms": o["textual_ms"],
            "textual_speedup": o["textual_ms"] / n["textual_ms"],
        })

    print(f"\n[bench] === results (median of {args.iterations} calls, {args.warmup} warmup) ===")
    for r in rows:
        print(
            f"  {r['model']:38s} visual {r['native_visual_ms']:7.1f}ms vs {r['onnx_visual_ms']:7.1f}ms "
            f"({r['visual_speedup']:.1f}x)   textual {r['native_textual_ms']:6.1f}ms vs "
            f"{r['onnx_textual_ms']:6.1f}ms ({r['textual_speedup']:.1f}x)"
        )

    if args.markdown:
        print("\n" + render_markdown(rows))

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"\n[bench] wrote {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
