#!/usr/bin/env python3
"""Real-model preflight gate for the native ML service (mlx / immich-ml-metal).

WHY THIS EXISTS
---------------
v1.5.29 lifted the `mlx<0.31.2` pin to allow 0.32.0 after a re-test that only
called `mlx_clip.image_encoder` in a ThreadPoolExecutor. That passed 1800x, yet
mlx 0.32.0 still hard-crashes the *real* service on the first `/predict` CLIP
call with `std::runtime_error: There is no Stream(cpu, 1) in current thread`
(issue #103). The proxy test and the fork's own `test_predict.py` (which runs in
STUB_MODE, no real inference) both missed it.

This gate boots the ACTUAL FastAPI service with real models loaded
(`STUB_MODE=false`) and fires concurrent `/predict` CLIP-visual requests, exactly
like Immich does. The crash is a C++ abort (SIGABRT), not a catchable Python
exception, so it kills the service process; the gate detects that via the child
exit and the abort line in the service's stderr.

It MUST be run on real Apple Silicon (Metal), so it cannot live in GitHub CI.
It is the mandatory gate before any mlx / ml change merges. A green
`image_encoder`-only or STUB_MODE test does NOT substitute for this.

USAGE
-----
  python3 scripts/ml-preflight.py \
      --python <ml-venv>/bin/python3 \
      --src <immich-ml-metal checkout> \
      --models-dir ~/.immich-accelerator/ml/models \
      --cache-dir  ~/.immich-accelerator/ml/cache

Exit 0 = the service survived real concurrent CLIP inference (pin is safe).
Exit 1 = the service crashed or misbehaved (do NOT ship this mlx).
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# A minimal valid baseline JPEG, embedded so the gate has no PIL/numpy dependency
# in whatever interpreter runs it (it only needs to decode to real pixels for CLIP
# to run inference on — content is irrelevant to reproducing the mlx crash).
_TINY_JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDFooorzzuCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAP/9k="


def tiny_jpeg() -> bytes:
    import base64

    return base64.b64decode(_TINY_JPEG_B64)


def predict_clip(base: str, image: bytes) -> str:
    """One real /predict CLIP-visual call, multipart, matching immich-accelerator
    ml-test. Returns 'ok' or raises."""
    boundary = "----iac-ml-preflight"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="entries"\r\n\r\n',
            json.dumps({"clip": {"visual": {"modelName": "ViT-B-32__openai"}}}).encode()
            + b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image"; filename="t.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n",
            image,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(
        f"{base}/predict",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read())
    raw = result.get("clip")
    emb = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(emb, list) or len(emb) < 100:
        raise RuntimeError(
            f"bad embedding: {type(emb).__name__} len={getattr(emb, '__len__', lambda: '?')()}"
        )
    return f"dim={len(emb)}"


def wait_healthy(base: str, proc: subprocess.Popen, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"service exited during startup (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
                if json.loads(r.read()).get("status") == "healthy":
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("service did not become healthy in time")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True, help="ml venv python to test")
    ap.add_argument(
        "--src", required=True, help="immich-ml-metal checkout (has src/main.py)"
    )
    ap.add_argument(
        "--models-dir", default=os.path.expanduser("~/.immich-accelerator/ml/models")
    )
    ap.add_argument(
        "--cache-dir", default=os.path.expanduser("~/.immich-accelerator/ml/cache")
    )
    ap.add_argument("--port", type=int, default=3991)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--requests", type=int, default=24)
    ap.add_argument("--startup-timeout", type=int, default=180)
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    env = {
        **os.environ,
        "STUB_MODE": "false",  # REAL models — the whole point
        "ML_PORT": str(args.port),
        "ML_HOST": "127.0.0.1",
        "ML_MODELS_DIR": args.models_dir,
        "ML_CACHE_DIR": args.cache_dir,
        "ML_CLIP_MODEL": "ViT-B-32__openai",
        "ML_FACE_MODEL": "buffalo_l",  # load face too: the #103 crash had both models in-process
        "ML_LOG_REQUESTS": "true",
    }
    err = open(os.path.join("/tmp", f"ml-preflight-{args.port}.err"), "w+b")
    print(
        f"[preflight] booting real ML service on {base} with STUB_MODE=false ...",
        flush=True,
    )
    proc = subprocess.Popen(
        [args.python, "-m", "src.main"],
        cwd=args.src,
        env=env,
        stdout=err,
        stderr=subprocess.STDOUT,
    )

    def stderr_tail() -> str:
        err.flush()
        err.seek(0)
        return err.read().decode(errors="replace")[-1500:]

    try:
        wait_healthy(base, proc, args.startup_timeout)
        print(
            f"[preflight] healthy — firing {args.requests} concurrent /predict CLIP calls "
            f"(concurrency={args.concurrency}) ...",
            flush=True,
        )
        image = tiny_jpeg()
        errors = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(predict_clip, base, image) for _ in range(args.requests)]
            for f in futs:
                try:
                    f.result()
                except Exception as e:
                    errors.append(repr(e))

        # The crash kills the service; detect via child death + connection errors.
        if proc.poll() is not None:
            tail = stderr_tail()
            abort = next(
                (
                    ln
                    for ln in tail.splitlines()
                    if "Stream(" in ln
                    or "libc++abi" in ln
                    or "uncaught exception" in ln
                ),
                "",
            )
            print(
                f"[preflight] FAIL — service DIED during inference (rc={proc.returncode})"
            )
            if abort:
                print(f"[preflight]   abort: {abort.strip()}")
            return 1
        if errors:
            print(
                f"[preflight] FAIL — {len(errors)}/{args.requests} predict calls errored"
            )
            for e in errors[:3]:
                print(f"[preflight]   {e}")
            return 1
        print(
            f"[preflight] PASS — {args.requests} real concurrent CLIP inferences, service alive, "
            f"no Stream abort"
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
