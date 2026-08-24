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

# macOS ships /usr/bin/python3 at 3.9, and that is what runs this gate on a
# release Mac. Without this, `str | None` in an annotation is evaluated at
# def time and raises TypeError on import, before the gate does anything.
from __future__ import annotations

import argparse
import json
import os
import pathlib
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



# A 320x96 JPEG reading "EXIT 42B". The OCR check needs an image that actually
# contains text: with a blank one the detector finds no regions, the box code
# never executes, and the gate passes on a path it never entered. That is
# exactly how the box-shape defect survived a green run.
_TEXT_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8Q"
    "EBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
    "EBAQEBAQEBD/wAARCABgAUADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUF"
    "BAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW"
    "V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi"
    "4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC"
    "AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVm"
    "Z2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq"
    "8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD9U6KKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKK"
    "KACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACi"
    "iigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACuW+Knjc/DP4YeL/iONM/tL/hFNB1DW/sXn+T9p+y27zeV5m1tm7Zt"
    "3bWxnOD0rqa5b4qeCP8AhZnww8YfDf8AtMab/wAJXoOoaH9sMHnfZvtVu8Pm+XuXft37tu5c4xkdaAOa8R/FTxX8ONFg8VfFfwlo"
    "Gj6AuoR2upajpfiCa/TTbeRWCXUoks4Ds87yo2xnaJd5O1Wqt4b+LnjzxbqOo+H9M+GdjYa1Z+H9G8RpZ6xrklsPI1G71KJIpjHa"
    "yNFMkWno7KFcB5mjziPzHzbX9nWC80JPBfiQeA4fCdxqKX+raJ4Y8HHRoNWCRkRxXP8ApUwZBJ5UhwAW8lUPylgej+Gvwo1XwL4j"
    "v/EeseNpvEM934e0nw3G89p5c3kafdalLDLLJ5jebK0WopG7YXc8DSYHmbEAIPhn8X7/AMT/AAZ0/wCNfxC0LSPCmkaloNr4jiS1"
    "1eXUTDZTWyz/AL0tbQ4kUNjagfJ6E5Arj0/aa8UTfCg/FZPg5c29rpzeIJNcs7zVxHJYxaXqUtk0EbLC4nvZfKZkt/lXKsnm/dZ+"
    "20L4IeG7f4KeEPgr4qu7vWLDwppWj2AurW5uNNkuJtPjiENwDbyiSM+ZCkgUSHBA5OAa4ex/Zm8T+HrHRtD8L/E60fRdK8S6z4ql"
    "0/xBpV7qy3d7d6hLd2rSO2oI5FqJflBJWSYfaGHmYIAOgvfjZ4tt/G+vaNa/DqxvPD/h7xfpXhC5votbc6g817aWE4uEsvsuwxRf"
    "2im//SNwjhlkxhdtev14Jrn7KumeIfi1qfxL1O+8Lyrf+LNJ8WQzHwuDrllNYWtjClvDqZuPlgdrAF18nlJ5kz826ve6ACiiigAo"
    "oooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooA"
    "KKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKK"
    "ACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD/"
    "2Q=="
)


def text_jpeg() -> bytes:
    import base64

    return base64.b64decode(_TEXT_JPEG_B64)


# The faces check needs an image that actually contains a face, for the same
# reason the OCR check needs one with text: a face-free image makes "found
# nothing" the correct answer, and the check can no longer tell that apart from
# a detector that found nothing because it never loaded.
#
# Fetched on demand and cached outside the repository, never committed, which
# is what scripts/native-ml-full-benchmark.py already does for its own test
# photos. An earlier version of this file embedded a base64 photograph instead.
# That was wrong twice over: it redistributed a photograph of identifiable
# people to every Homebrew user, since render-formula.sh installs this tree
# wholesale, and it recorded no source or licence for it. Raised by @RxChi1d
# in #167.
FACE_IMAGE = (
    "http://images.cocodataset.org/val2017/000000000785.jpg",
    "COCO val2017 000000000785.jpg - a skier, one clearly visible face. Same "
    "source set as the benchmark photos above, fetched rather than "
    "redistributed: COCO's annotations are CC BY 4.0 but the images themselves "
    "are Flickr-sourced and subject to Flickr's terms, so this repository "
    "links to it and does not carry a copy.",
)
FACE_CACHE = pathlib.Path.home() / ".cache" / "immich-accelerator" / "preflight"


def face_jpeg(override: str | None = None) -> bytes:
    """The face image, from --face-image or the cache, downloading if needed.

    Failure to obtain it is fatal rather than a skip. Skipping is precisely the
    false green this image exists to prevent: a gate that quietly drops the
    faces check reports success having never asked the detector anything.
    """
    if override:
        return pathlib.Path(override).read_bytes()

    url, provenance = FACE_IMAGE
    cached = FACE_CACHE / url.rsplit("/", 1)[-1]
    if cached.is_file():
        return cached.read_bytes()

    FACE_CACHE.mkdir(parents=True, exist_ok=True)
    print(f"[preflight] fetching face image <- {url}", flush=True)
    print(f"[preflight]   {provenance}", flush=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
    except Exception as exc:
        raise RuntimeError(
            f"could not fetch the face image from {url}: {exc}. "
            f"Pass --face-image PATH to run the gate offline; it needs a "
            f"photograph containing a face the detector can find."
        ) from exc
    # A JPEG starts FF D8 FF. Without this check a captive portal or proxy
    # answering with a 200 HTML login page gets cached under a .jpg name, and
    # every later run short-circuits to it and fails the faces check with
    # "found no faces in an image that contains one" -- a red on the mlx gate
    # that looks exactly like a detector regression and is not one.
    if not data.startswith(b"\xff\xd8\xff"):
        raise RuntimeError(
            f"{url} did not return a JPEG (got {len(data)} bytes starting "
            f"{data[:8]!r}). Nothing was cached. A captive portal or proxy "
            f"answering for it is the usual cause; --face-image PATH works "
            f"offline."
        )
    # Written via a temporary name so an interrupted download cannot leave a
    # truncated file that every later run then reads as valid.
    tmp = cached.with_suffix(cached.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(cached)
    return data

# Every task the service exposes, not just CLIP. Firing only CLIP is how three
# new inference paths passed this gate while stock OCR returned a shape the
# response schema rejects and the stock face detector reported zero faces for
# every image: the gate was green and had executed neither.
TASKS = {
    "clip": {"clip": {"visual": {"modelName": "ViT-B-32__openai"}}},
    # The nested shape the service actually reads (main.py: task_config
    # ["detection"]["options"]["minScore"], ["recognition"]["modelName"]). The
    # flat form parses fine and the thresholds inside it reach nothing, so the
    # gate would run at whatever the service defaults to while believing it had
    # asked for something else.
    "faces": {
        "facial-recognition": {
            "detection": {"options": {"minScore": 0.3}},
            "recognition": {"modelName": "buffalo_l"},
        }
    },
    "ocr": {
        "ocr": {
            "detection": {"options": {"minScore": 0.3}},
            "recognition": {"modelName": "PP-OCRv5_mobile", "options": {"minScore": 0.3}},
        }
    },
}


def check_response(task: str, result: dict) -> str:
    """Confirm the answer has the shape Immich expects, not merely a 200.

    The stock OCR defect returned HTTP 200 from the model and only failed in
    response validation, so a status check alone would have called it healthy.
    """
    if task == "clip":
        raw = result.get("clip")
        emb = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(emb, list) or len(emb) < 100:
            raise RuntimeError(f"bad embedding: {type(emb).__name__}")
        return f"dim={len(emb)}"
    if task == "faces":
        faces = result.get("facial-recognition")
        if faces is None:
            raise RuntimeError("no facial-recognition key in response")
        if not isinstance(faces, list):
            raise RuntimeError(f"faces should be a list, got {type(faces).__name__}")
        # The image carries a face, so an empty list means the detector found
        # nothing in a picture of a person: either it failed to load, or it
        # loaded and does not work. Accepting the empty list is what let a
        # detector that returned zero faces for every image pass this gate.
        if not faces:
            raise RuntimeError("found no faces in an image that contains one")
        return f"faces={len(faces)}"
    ocr = result.get("ocr")
    if not isinstance(ocr, dict):
        raise RuntimeError(f"ocr should be an object, got {type(ocr).__name__}")
    box = ocr.get("box", [])
    # Flat coordinates. A list of per-box lists is what the schema rejects.
    if box and not all(isinstance(v, (int, float)) for v in box):
        raise RuntimeError("ocr box must be a flat list of numbers")
    texts = ocr.get("text", [])
    # An OCR run over an image with text that returns nothing has not exercised
    # the box path, so a pass here would mean nothing.
    if not texts:
        raise RuntimeError("read no text from an image that contains text")
    return f"text={len(texts)}"


def predict_task(base: str, image: bytes, task: str = "clip",
                 face_override: str | None = None) -> str:
    """One real /predict call, multipart, matching immich-accelerator ml-test."""
    if task == "ocr":
        image = text_jpeg()
    elif task == "faces":
        image = face_jpeg(face_override)
    boundary = "----iac-ml-preflight"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="entries"\r\n\r\n',
            json.dumps(TASKS[task]).encode() + b"\r\n",
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
    with urllib.request.urlopen(req, timeout=120) as r:
        result = json.loads(r.read())
    return check_response(task, result)


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
    ap.add_argument(
        "--face-image",
        help="a photograph containing a face, for running the gate offline. "
        "Without it the image is fetched once and cached outside the repo.",
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
            f"[preflight] healthy — firing {args.requests} concurrent CLIP "
            f"/predict calls plus one each of {', '.join(t for t in TASKS if t != 'clip')} "
            f"(concurrency={args.concurrency}) ...",
            flush=True,
        )
        image = tiny_jpeg()
        errors = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            # The full CLIP count, unchanged: this gate exists to reproduce a
            # crash that only appears under concurrent CLIP inference, and
            # sharing the budget with the other tasks would quietly reduce the
            # pressure it is here to apply. Faces and OCR are added on top so
            # they are exercised without weakening the original check.
            futs = [
                ex.submit(predict_task, base, image, "clip")
                for _ in range(args.requests)
            ]
            futs += [
                ex.submit(predict_task, base, image, task, args.face_image)
                for task in TASKS
                if task != "clip"
            ]
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
                f"[preflight] FAIL — {len(errors)}/{len(futs)} predict calls errored"
            )
            for e in errors[:3]:
                print(f"[preflight]   {e}")
            return 1
        print(
            f"[preflight] PASS — {args.requests} real concurrent CLIP inferences "
            f"plus {len(futs) - args.requests} other task calls, service alive, "
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
