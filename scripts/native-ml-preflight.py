#!/usr/bin/env python3
"""Real-model concurrent preflight gate for native-ml (immich-ml-native).

WHY THIS EXISTS
---------------
scripts/ml-preflight.py gates the Python `ml` submodule service against real
concurrent /predict load — it boots STUB_MODE=false and fires concurrent CLIP
calls because synthetic proxies (image_encoder loops, STUB_MODE tests) missed
a real mlx crash that only reproduced under the actual FastAPI service (#103).
That gate explicitly does not, and cannot, cover native-ml: a separate Swift
binary with its own MLX runtime, its own HTTP server (Server.swift, via
Network.framework), and its own model-residency/switching logic
(Models.zoo(for:), an NSCondition-guarded single-resident-model cache). None
of that machinery is exercised by `zootest` (a single-threaded CLI harness) or
by any unit test — only by the real server under real concurrent requests,
exactly the class of bug the Python gate exists to catch on the other service.

This gate boots the ACTUAL release binary (`swift build -c release`, the same
build produced for shipping — see native-ml/scripts/build_bundle.sh) with the
real HTTP server, and fires concurrent /predict calls (both clip.visual and
clip.textual) at it for a sample of real registry models, using weights
already on disk (no stub, no mock). A crash presents as the process dying
(SIGABRT/SIGSEGV/SIGILL/Swift fatal error) — detected via child exit plus an
abort signature in stderr, the same technique as ml-preflight.py.

USAGE
-----
  python3 scripts/native-ml-preflight.py
  python3 scripts/native-ml-preflight.py --models all
  python3 scripts/native-ml-preflight.py --binary native-ml/.build/release/immich-ml-native

Exit 0 = the server survived real concurrent CLIP inference across the tested
         models (safe to ship this native-ml / mlx-swift change).
Exit 1 = the server crashed or misbehaved (do NOT ship this change).
"""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Same minimal valid baseline JPEG as ml-preflight.py, so this gate has no
# PIL/numpy dependency — content is irrelevant, it just needs to decode.
_TINY_JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDFooorzzuCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAP/9k="

# Models.defaultClip in the Swift service: the always-resident mlx CLIPEncoder
# path, which is a different implementation from ZooCLIP/SigLIPNative and needs
# covering in its own right, including in the mixed-load stage.
DEFAULT_CLIP = "ViT-B-32__openai"

# One model per architecture family/scale actually in SigLIPRegistry (see
# SigLIPNative.swift), spanning the axes most likely to expose a real
# concurrency bug: smallest vs largest tower, v1 vs v2, and the mlx
# fast-path default CLIP model (a different code path entirely — CLIPEncoder,
# not ZooCLIP/SigLIPNative — that's always resident and must keep working
# while zoo models load/switch around it). Not the full 17: that's what
# --models all is for (run before an actual native-ml/mlx-swift release).
DEFAULT_MODELS = [
    DEFAULT_CLIP,
    "ViT-B-16-SigLIP-256__webli",
    "ViT-SO400M-16-SigLIP2-384__webli",
    "ViT-L-16-SigLIP2-512__webli",
]

# Keep in sync with SigLIPRegistry.models in SigLIPNative.swift.
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
ALL_MODELS = [DEFAULT_CLIP] + ALL_SIGLIP_MODELS

ABORT_SIGNATURES = (
    "Stream(",
    "libc++abi",
    "uncaught exception",
    "Fatal error",
    "Illegal instruction",
    "Precondition failed",
    "Segmentation fault",
    "EXC_BAD_INSTRUCTION",
    "EXC_BAD_ACCESS",
)


def tiny_jpeg() -> bytes:
    return base64.b64decode(_TINY_JPEG_B64)


def predict_raw(
    base: str, entries: dict, image: bytes | None, text: str | None, timeout: int = 60
) -> dict:
    """One real /predict call for an arbitrary entries dict, matching Immich's
    own multipart wire format (see Server.swift/Predict.swift)."""
    boundary = "----native-ml-preflight"
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="entries"\r\n\r\n',
        json.dumps(entries).encode() + b"\r\n",
    ]
    if image is not None:
        parts += [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image"; filename="t.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n",
            image,
            b"\r\n",
        ]
    if text is not None:
        parts += [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="text"\r\n\r\n',
            text.encode() + b"\r\n",
        ]
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{base}/predict",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def predict(
    base: str, model_name: str, kind: str, image: bytes, text: str, timeout: int = 60
) -> str:
    """One real /predict call (visual or textual) for a specific zoo model."""
    result = predict_raw(
        base,
        {"clip": {kind: {"modelName": model_name}}},
        image if kind == "visual" else None,
        text if kind != "visual" else None,
        timeout,
    )
    raw = result.get("clip")
    emb = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(emb, list) or len(emb) < 8:
        raise RuntimeError(f"bad embedding for {model_name}/{kind}: {emb!r}"[:200])
    return f"{model_name}/{kind} dim={len(emb)}"


def predict_face(base: str, image: bytes, timeout: int = 60) -> str:
    """One real /predict call for facial-recognition — Vision framework, Metal-backed
    on its own command queue, run concurrently with clip below to reproduce the
    MLX-vs-Vision-framework Metal race (see GPULock.swift)."""
    predict_raw(
        base,
        {"facial-recognition": {"detection": {"options": {"minScore": 0.5}}}},
        image,
        None,
        timeout,
    )
    return "facial-recognition ok"


def predict_ocr(base: str, image: bytes, timeout: int = 60) -> str:
    """One real /predict call for ocr — also Vision framework / Metal-backed."""
    predict_raw(
        base,
        {"ocr": {"detection": {"options": {"minScore": 0.0}}}},
        image,
        None,
        timeout,
    )
    return "ocr ok"


def downloading(base: str) -> dict | None:
    """The /health "downloading" block, or None if nothing is being fetched."""
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
            return json.loads(r.read()).get("downloading")
    except Exception:
        return None


def warm_up(
    base: str,
    name: str,
    image: bytes,
    text: str,
    proc: subprocess.Popen,
    quiet_timeout: int,
    stall_timeout: int = 900,
    hard_cap: int = 7200,
) -> None:
    """Cold-load a model, waiting out a first-use download instead of failing on it.

    The naive version — a plain predict() with a fixed timeout — cannot pass on
    a cold cache: the largest checkpoints are gigabytes and the fetch happens
    inside this first request, so the gate timed out on a download that was
    working perfectly and reported it as a failure. Time spent downloading is
    therefore not counted; the timeout applies only to silence, so a crash or a
    genuinely wedged load still fails, and a slow network never does.
    """
    import threading

    outcome: dict = {}

    def call() -> None:
        try:
            predict(base, name, "visual", image, text, timeout=24 * 3600)
            predict(base, name, "textual", image, text, timeout=24 * 3600)
        except Exception as e:  # surfaced on the main thread below
            outcome["error"] = e

    done = threading.Event()
    worker = threading.Thread(target=lambda: (call(), done.set()), daemon=True)
    worker.start()

    started = time.time()
    deadline = started + quiet_timeout
    seen = None
    while not done.wait(5):
        if proc.poll() is not None:
            return  # died: the caller's died_with_abort reports the detail

        # Nothing below may extend time without bound. The first version of
        # this reset the deadline on the mere presence of a downloading block,
        # so a transfer wedged at 3/8 chunks pushed the deadline out every five
        # seconds forever while the worker thread sat on a 24-hour request
        # timeout. A gate that hangs is worse than one that fails: the operator
        # kills it and ships with no verdict from the check that exists to
        # produce one.
        if time.time() - started > hard_cap:
            raise TimeoutError(
                f"{name} did not finish loading within the {hard_cap}s hard cap"
            )

        progress = downloading(base)
        if progress:
            if progress != seen:
                # Only observable movement buys more time, and only up to the
                # stall timeout, which is generous because chunks of a
                # multi-gigabyte checkpoint complete minutes apart.
                seen = progress
                deadline = time.time() + stall_timeout
                print(
                    f"[preflight]   downloading {progress.get('model', name)}: "
                    f"{progress.get('files_done')}/{progress.get('files_total')}",
                    flush=True,
                )
            elif time.time() > deadline:
                at = f"{progress.get('files_done')}/{progress.get('files_total')}"
                raise TimeoutError(
                    f"{name} download stalled: no progress for {stall_timeout}s "
                    f"(stuck at {at})"
                )
        elif time.time() > deadline:
            raise TimeoutError(
                f"{name} did not load within {quiet_timeout}s and reported no download"
            )
    if "error" in outcome:
        raise outcome["error"]


def check_bind_conflict(
    binary: str, port: int, env: dict, timeout: int = 120
) -> str | None:
    """Start a second service on an occupied port; it must exit, not linger.

    NWListener reports a bind conflict asynchronously, so a server that does not
    watch its listener state stays alive forever holding nothing. That shipped:
    a release Mac accumulated six idle instances over four days while ml.pid
    named one of them and the real listener was an older process, which made
    `restart` a no-op. Nothing else in the suite covers it, because every other
    stage is careful to use a free port. Returns None on success, else a reason.
    """
    second = subprocess.Popen(
        [binary, "serve", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        rc = second.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        second.kill()
        second.wait()
        return (
            f"a second service on the already-bound port {port} was still running after "
            f"{timeout}s instead of exiting (this is the leaked-process bug)"
        )
    if rc == 0:
        return f"a second service on port {port} exited 0, as though it had bound successfully"
    return None


def wait_healthy(base: str, proc: subprocess.Popen, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"service exited during startup (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
                health = json.loads(r.read())
                # "healthy" needs arcface loaded too; this gate only cares
                # about clip, so accept "degraded" as long as clip itself is ok.
                if health.get("checks", {}).get("clip") == "ok":
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("service did not become healthy in time")


def build_release(native_ml_dir: str) -> str:
    print(f"[preflight] swift build -c release in {native_ml_dir} ...", flush=True)
    subprocess.run(["swift", "build", "-c", "release"], cwd=native_ml_dir, check=True)
    return os.path.join(native_ml_dir, ".build", "release", "immich-ml-native")


def ensure_metallib(binary: str, native_ml_dir: str) -> None:
    """mlx.metallib must sit beside the binary at runtime (see
    native-ml/scripts/build_bundle.sh, which does the same copy for a real
    release bundle) — swift build does not place it there itself. Search the
    usual spots; on a dev machine the debug build often already has one from
    an earlier `swift build` and it's config-independent (same compiled Metal
    shaders regardless of Swift optimization level), so reuse it."""
    dst = os.path.join(os.path.dirname(os.path.abspath(binary)), "mlx.metallib")
    if os.path.isfile(dst):
        return
    candidates = subprocess.run(
        ["find", "/opt/homebrew", native_ml_dir, "-name", "mlx.metallib"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    candidates = [c for c in candidates if os.path.abspath(c) != dst]
    if not candidates:
        raise RuntimeError(
            "mlx.metallib not found anywhere (checked /opt/homebrew and the native-ml checkout); "
            "build the debug target at least once, or set it up per native-ml/scripts/build_bundle.sh"
        )
    print(f"[preflight] copying {candidates[0]} -> {dst}", flush=True)
    import shutil

    shutil.copy2(candidates[0], dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--native-ml-dir",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "native-ml"
        ),
        help="native-ml checkout (has Package.swift)",
    )
    ap.add_argument(
        "--binary", help="pre-built binary path; skips swift build -c release"
    )
    ap.add_argument(
        "--clip-dir", default=os.path.expanduser("~/.cache/immich-ml-native/clip")
    )
    ap.add_argument(
        "--arcface",
        default=None,
        help="omit to let the binary use its own default search",
    )
    ap.add_argument("--port", type=int, default=3998)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument(
        "--requests",
        type=int,
        default=16,
        help="concurrent requests per model, split visual/textual",
    )
    ap.add_argument(
        "--mixed-image",
        default="/tmp/native-ml-bench.jpg",
        help="realistic-resolution image for the mixed clip+vision stage (see "
        "scripts/native-ml-siglip-benchmark.py's ensure_bench_image) — a tiny image resolves "
        "clip.visual in a few ms and closes the Metal collision window almost instantly, "
        "hiding the crash this stage exists to catch. Falls back to the tiny synthetic JPEG "
        "if the file doesn't exist.",
    )
    ap.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="comma-separated model names, or 'all' for the full registry (slow: first-load per model)",
    )
    ap.add_argument("--startup-timeout", type=int, default=120)
    ap.add_argument(
        "--warmup-timeout",
        type=int,
        default=240,
        help="how long a cold model load may go quiet before it counts as wedged. "
        "Time spent downloading does not count against it (see warm_up), so this "
        "does not need to cover a multi-GB first fetch.",
    )
    ap.add_argument(
        "--skip-bind-check",
        action="store_true",
        help="skip the second-instance-on-a-bound-port stage (costs one extra model load)",
    )
    args = ap.parse_args()

    models = (
        ALL_MODELS
        if args.models == "all"
        else [m.strip() for m in args.models.split(",") if m.strip()]
    )

    binary = args.binary or build_release(args.native_ml_dir)
    if not os.path.isfile(binary):
        print(f"[preflight] FAIL — binary not found: {binary}")
        return 1
    if not os.path.isdir(args.clip_dir):
        print(
            f"[preflight] FAIL — clip dir not found: {args.clip_dir} "
            f"(run native-ml/scripts/install_native.sh first)"
        )
        return 1
    ensure_metallib(binary, args.native_ml_dir)

    base = f"http://127.0.0.1:{args.port}"
    env = {**os.environ, "ML_CLIP_DIR": args.clip_dir}
    if args.arcface:
        env["ML_ARCFACE"] = args.arcface

    err = open(os.path.join("/tmp", f"native-ml-preflight-{args.port}.err"), "w+b")
    print(
        f"[preflight] booting real native-ml service on {base} (release build) ...",
        flush=True,
    )
    proc = subprocess.Popen(
        [binary, "serve", str(args.port)], env=env, stdout=err, stderr=subprocess.STDOUT
    )

    def stderr_tail() -> str:
        err.flush()
        err.seek(0)
        return err.read().decode(errors="replace")[-2000:]

    def died_with_abort(prefix: str) -> bool:
        if proc.poll() is None:
            return False
        tail = stderr_tail()
        abort = next(
            (
                ln
                for ln in tail.splitlines()
                if any(sig in ln for sig in ABORT_SIGNATURES)
            ),
            "",
        )
        print(f"[preflight] FAIL — service DIED {prefix} (rc={proc.returncode})")
        if abort:
            print(f"[preflight]   abort: {abort.strip()}")
        elif tail.strip():
            print(f"[preflight]   stderr tail: {tail.strip()[-500:]}")
        return True

    try:
        wait_healthy(base, proc, args.startup_timeout)
        image = tiny_jpeg()
        text = "a photo of a cat"

        for i, name in enumerate(models, 1):
            print(
                f"[preflight] [{i}/{len(models)}] {name}: warming up (model load/switch) ...",
                flush=True,
            )
            try:
                # Cold load: evicts whatever's resident, fetches the checkpoint
                # if this Mac has never run the model, and reads a multi-GB
                # safetensors file off disk (Models.zoo(for:), see Models.swift)
                # — measured minutes, not seconds, for the largest towers back
                # to back. warm_up waits out the download part; once resident,
                # inference itself is fast, hence the tighter per-request
                # timeout on the concurrent batch below.
                warm_up(base, name, image, text, proc, args.warmup_timeout)
            except Exception as e:
                if died_with_abort(f"loading {name}"):
                    return 1
                print(f"[preflight] FAIL — {name} warmup errored: {e!r}")
                return 1

            print(
                f"[preflight] [{i}/{len(models)}] {name}: firing {args.requests} concurrent "
                f"predict calls (concurrency={args.concurrency}) ...",
                flush=True,
            )
            errors = []
            with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                futs = [
                    ex.submit(
                        predict,
                        base,
                        name,
                        "visual" if j % 2 == 0 else "textual",
                        image,
                        text,
                    )
                    for j in range(args.requests)
                ]
                for f in futs:
                    try:
                        f.result()
                    except Exception as e:
                        errors.append(repr(e))

            if died_with_abort(f"under concurrent load on {name}"):
                return 1
            if errors:
                print(
                    f"[preflight] FAIL — {name}: {len(errors)}/{args.requests} predict calls errored"
                )
                for e in errors[:3]:
                    print(f"[preflight]   {e}")
                return 1
            print(
                f"[preflight] [{i}/{len(models)}] {name}: OK ({args.requests} concurrent calls, no crash)"
            )

        # Mixed-load stage: fires clip.visual concurrently with
        # facial-recognition and ocr — the actual crash-reproducing shape.
        # Server.swift's concurrent connection queue mixes MLX (clip) with
        # Vision-framework work (faces/ocr) across simultaneous requests;
        # the per-model loop above only ever exercises clip against clip,
        # so it can pass while this combination still crashes (see
        # GPULock.swift). Reuses whichever model is already resident from
        # the loop above — no extra cold-load cost.
        # Both engines, not just the resident one. CLIPEncoder (the default
        # ViT-B-32 path) and SigLIPNative are separate implementations, and the
        # commit that moved preprocessing outside withMetalLock changed both.
        # Running only models[-1] exercised the zoo path and left the engine
        # every stock install actually uses covered by nothing but clip against
        # clip, which cannot reproduce this crash shape.
        mixed_models = [models[-1]]
        if DEFAULT_CLIP not in mixed_models:
            mixed_models.append(DEFAULT_CLIP)
        mixed_image = image
        if os.path.isfile(args.mixed_image):
            with open(args.mixed_image, "rb") as f:
                mixed_image = f.read()
        else:
            print(
                f"[preflight] warning: {args.mixed_image} not found, using tiny test image "
                f"(closes the collision window almost instantly — see --mixed-image help)"
            )
        for mixed_model in mixed_models:
            print(
                f"[preflight] mixed load: {mixed_model} clip.visual concurrent with "
                f"facial-recognition + ocr ({args.requests} calls, concurrency={args.concurrency}, "
                f"image={len(mixed_image)} bytes) ...",
                flush=True,
            )
            errors = []
            with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                futs = []
                for j in range(args.requests):
                    m = j % 3
                    if m == 0:
                        futs.append(
                            ex.submit(
                                predict, base, mixed_model, "visual", mixed_image, text
                            )
                        )
                    elif m == 1:
                        futs.append(ex.submit(predict_face, base, mixed_image))
                    else:
                        futs.append(ex.submit(predict_ocr, base, mixed_image))
                for f in futs:
                    try:
                        f.result()
                    except Exception as e:
                        errors.append(repr(e))

            if died_with_abort(
                f"under mixed clip+vision concurrent load ({mixed_model})"
            ):
                return 1
            if errors:
                print(
                    f"[preflight] FAIL — mixed load {mixed_model}: "
                    f"{len(errors)}/{args.requests} calls errored"
                )
                for e in errors[:3]:
                    print(f"[preflight]   {e}")
                return 1
            print(
                f"[preflight] mixed load {mixed_model}: OK "
                f"({args.requests} concurrent calls, no crash)"
            )

        if args.skip_bind_check:
            print("[preflight] bind conflict: SKIPPED (--skip-bind-check)")
        else:
            print(
                f"[preflight] bind conflict: starting a second service on the "
                f"already-bound port {args.port} (must exit, not linger) ...",
                flush=True,
            )
            reason = check_bind_conflict(binary, args.port, env)
            if reason:
                print(f"[preflight] FAIL — {reason}")
                return 1
            print("[preflight] bind conflict: OK (second instance exited)")

        print(
            f"[preflight] PASS — {len(models)} model(s), {args.requests} concurrent real predict "
            f"calls each, service alive throughout, no abort"
        )
        return 0
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        err.close()


if __name__ == "__main__":
    sys.exit(main())
