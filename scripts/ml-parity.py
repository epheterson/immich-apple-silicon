#!/usr/bin/env python3
"""Run the same images through several ML engines and compare what they answer.

WHY THIS EXISTS
---------------
The encoding side has `immich-accelerator compare`: give it a video and it
shows you what each setting costs and what it looks like. Machine learning had
nothing equivalent. The claim "this Mac produces the same search, faces and
text as Docker would" rested on reading the code and on a gate that only asks
whether a request succeeds, not whether the answer matches.

This asks the question directly. It sends identical images, with identical
model names, to Immich's own container and to whichever engines you name, and
reports how far apart the answers are.

WHAT IT CANNOT TELL YOU
-----------------------
Nothing here says an engine is correct. It says two engines agree, or do not.
If both are wrong in the same way this reports perfect parity. The reference
is only a reference because it is what Docker users get.

Cosine similarity near 1.0 means the embeddings point the same way, which is
what search ranking depends on; it does not mean the floats are identical, and
they will not be across different runtimes.

TYPICAL USE
-----------
Start Immich's own ML container as the reference:

    docker run -d --name ml-parity-stock -p 3013:3003 \\
      -v ml-parity-cache:/cache \\
      ghcr.io/immich-app/immich-machine-learning:v3.0.2

Then:

    ./scripts/ml-parity.py \\
      --reference stock=http://localhost:3013 \\
      --engine ours=http://localhost:3003 \\
      --images ~/photos/*.jpg --out parity.html

Use the same Immich version as the server you are comparing against, or the
models differ and the comparison is measuring the wrong thing.
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import math
import pathlib
import statistics
import sys
import time
import urllib.request

from _predict import predict as _post

# The model names Immich itself asks for, taken from a real worker's requests.
# Every engine is asked for the same ones: comparing two engines running
# different models measures the models, not the engines.
CLIP_MODEL = "ViT-B-32__openai"
FACE_MODEL = "buffalo_l"
OCR_MODEL = "PP-OCRv5_mobile"

TASKS = {
    "clip": {"clip": {"visual": {"modelName": CLIP_MODEL}}},
    "faces": {
        "facial-recognition": {
            "detection": {"modelName": FACE_MODEL, "options": {"minScore": 0.3}},
            "recognition": {"modelName": FACE_MODEL},
        }
    },
    "ocr": {
        "ocr": {
            "detection": {"modelName": OCR_MODEL, "options": {"minScore": 0.3}},
            "recognition": {"modelName": OCR_MODEL, "options": {"minScore": 0.3}},
        }
    },
}


def predict(base: str, task: str, image: bytes, timeout: int) -> tuple[dict, float]:
    """One /predict call, multipart, the same shape the worker sends.

    Wraps the shared builder in _predict.py to time the round trip, which is
    the one thing this script needs and the gates do not.
    """
    started = time.time()
    result = _post(base, TASKS[task], image=image, timeout=timeout, filename="i.jpg")
    return result, time.time() - started


def embedding(result: dict) -> list[float]:
    """CLIP embeddings come back as a JSON string from some engines and a list
    from others. Both are the same numbers."""
    raw = result.get("clip")
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raise RuntimeError(f"no embedding: {type(raw).__name__}")
    return [float(v) for v in raw]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise RuntimeError(f"embedding length differs: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        raise RuntimeError("zero-length embedding")
    return dot / (na * nb)


def iou(a: dict, b: dict) -> float:
    """Overlap of two face boxes, in Immich's own {x1,y1,x2,y2} shape."""
    ax1, ay1, ax2, ay2 = a["x1"], a["y1"], a["x2"], a["y2"]
    bx1, by1, bx2, by2 = b["x1"], b["y1"], b["x2"], b["y2"]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union else 0.0


# The overlap two boxes need before they are called the same face. 0.5 is what
# COCO and WIDER FACE use, and every detection number this script reports is a
# public claim, so it uses the convention a reader would assume rather than one
# of our own.
#
# It used to accept any overlap above zero. A box on someone's shoulder that
# clipped the edge of Immich's box on a face counted as a face we found, which
# inflated the recall figure, and the accompanying "worst overlap 0.393" was
# itself the evidence: pairs well below any accepted threshold were in the
# population being averaged.
MATCH_IOU = 0.5


def compare_faces(ref: list, other: list) -> dict:
    """Match boxes greedily by overlap and report how well they line up.

    Counts alone are not enough: two engines can each find one face in
    different places, and a count comparison calls that agreement. Overlap
    alone is not enough either, which is what MATCH_IOU is for.
    """
    ref_boxes = [f["boundingBox"] for f in ref]
    other_boxes = [f["boundingBox"] for f in other]
    # Best overlap first, not reference order. Walking the reference list let
    # face A take our box X on a 0.20 overlap while face B, which overlapped X
    # at 0.90, went unmatched: both the match count and the mean were then
    # understated, and that mean is what the docs quote.
    pairs = sorted(
        ((iou(rb, ob), i, j)
         for i, rb in enumerate(ref_boxes)
         for j, ob in enumerate(other_boxes)),
        reverse=True,
    )
    used_ref, used_other, overlaps = set(), set(), []
    for score, i, j in pairs:
        # Sorted best-first, so the first pair below the bar ends the walk.
        if score < MATCH_IOU:
            break
        if i in used_ref or j in used_other:
            continue
        used_ref.add(i)
        used_other.add(j)
        overlaps.append(score)
    return {
        "ref_count": len(ref),
        "count": len(other),
        "matched": len(overlaps),
        "mean_iou": statistics.fmean(overlaps) if overlaps else 0.0,
        "min_iou": min(overlaps) if overlaps else 0.0,
    }


def compare_ocr(ref: dict, other: dict) -> dict:
    """Compare the text read, not the boxes: a user searches for words."""

    def norm(d):
        return [t.strip().lower() for t in (d.get("text") or []) if t.strip()]

    a, b = norm(ref), norm(other)
    # Multisets. len(a) counted duplicates while the intersection could not, so
    # an engine that read the same two words the reference did scored 0.5
    # recall while also being reported as an identical read.
    ca, cb = collections.Counter(a), collections.Counter(b)
    shared = sum((ca & cb).values())
    return {
        "ref_count": len(a),
        "count": len(b),
        "shared": shared,
        "only_ref": sorted((ca - cb).elements())[:5],
        "only_other": sorted((cb - ca).elements())[:5],
    }


def run(images, engines, reference, tasks, timeout):
    """Returns rows of {image, task, engine, metrics...}, and per-engine timings."""
    rows = []
    timings = {name: {t: [] for t in tasks} for name in engines}
    for path in images:
        data = path.read_bytes()
        for task in tasks:
            answers = {}
            for name, base in engines.items():
                try:
                    result, secs = predict(base, task, data, timeout)
                    answers[name] = result
                    timings[name][task].append(secs)
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    answers[name] = RuntimeError(str(exc)[:200])

            ref_answer = answers[reference]
            for name in engines:
                if name == reference:
                    continue
                row = {"image": path.name, "task": task, "engine": name}
                got = answers[name]
                if isinstance(got, Exception) or isinstance(ref_answer, Exception):
                    which = reference if isinstance(ref_answer, Exception) else name
                    err = ref_answer if isinstance(ref_answer, Exception) else got
                    row["error"] = f"{which}: {err}"
                    rows.append(row)
                    continue
                try:
                    if task == "clip":
                        row["cosine"] = cosine(embedding(ref_answer), embedding(got))
                    elif task == "faces":
                        row.update(
                            compare_faces(
                                ref_answer.get("facial-recognition") or [],
                                got.get("facial-recognition") or [],
                            )
                        )
                    else:
                        row.update(
                            compare_ocr(
                                ref_answer.get("ocr") or {}, got.get("ocr") or {}
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    row["error"] = str(exc)[:200]
                rows.append(row)
    return rows, timings


def summarise(rows, timings, reference):
    """One line per engine per task, plus the worst case, which is the number
    that matters: a mean hides the one image an engine got wrong."""
    out = []
    engines = sorted({r["engine"] for r in rows})
    for engine in engines:
        for task in ("clip", "faces", "ocr"):
            these = [r for r in rows if r["engine"] == engine and r["task"] == task]
            if not these:
                continue
            errors = [r for r in these if "error" in r]
            ok = [r for r in these if "error" not in r]
            line = {
                "engine": engine,
                "task": task,
                "images": len(these),
                "errors": len(errors),
            }
            if task == "clip" and ok:
                cos = [r["cosine"] for r in ok]
                line["mean_cosine"] = statistics.fmean(cos)
                line["worst_cosine"] = min(cos)
            elif task == "faces" and ok:
                line["count_matches"] = sum(
                    1 for r in ok if r["ref_count"] == r["count"]
                )
                # Weighted by matched faces. A mean of per-image means gives a
                # one-face image the same weight as a ten-face one, and the
                # published figure claims to be the average overlap of matched
                # boxes, which is a different number.
                matched_total = sum(r["matched"] for r in ok)
                line["mean_iou"] = (
                    sum(r["mean_iou"] * r["matched"] for r in ok) / matched_total
                    if matched_total else 0.0
                )
                line["worst_iou"] = min(
                    (r["min_iou"] for r in ok if r["matched"]), default=0.0
                )
            elif task == "ocr" and ok:
                line["exact_sets"] = sum(
                    1 for r in ok if not r["only_ref"] and not r["only_other"]
                )
                totals = sum(r["ref_count"] for r in ok)
                line["recall"] = (
                    sum(r["shared"] for r in ok) / totals if totals else 0.0
                )
            secs = timings.get(engine, {}).get(task, [])
            ref_secs = timings.get(reference, {}).get(task, [])
            if secs:
                line["median_s"] = statistics.median(secs)
            if ref_secs:
                line["ref_median_s"] = statistics.median(ref_secs)
            out.append(line)
    return out


def write_report(path, rows, summary, engines, reference, images):
    def esc(v):
        return html.escape(str(v))

    def table(headers, body_rows):
        head = "".join(f"<th>{esc(h)}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
            for r in body_rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    sum_headers = ["engine", "task", "images", "errors", "agreement", "speed"]
    sum_rows = []
    for s in summary:
        if s["task"] == "clip":
            agree = (
                f"cosine mean {s.get('mean_cosine', 0):.5f}, "
                f"worst {s.get('worst_cosine', 0):.5f}"
            )
        elif s["task"] == "faces":
            agree = (
                f"same count on {s.get('count_matches', 0)}/{s['images']}, "
                f"IoU mean {s.get('mean_iou', 0):.3f}, "
                f"worst {s.get('worst_iou', 0):.3f}"
            )
        else:
            agree = (
                f"identical text on {s.get('exact_sets', 0)}/{s['images']}, "
                f"recall {s.get('recall', 0):.3f}"
            )
        speed = ""
        if "median_s" in s:
            speed = f"{s['median_s']:.2f}s"
            if s.get("ref_median_s"):
                speed += f" vs {s['ref_median_s']:.2f}s reference"
        sum_rows.append(
            [s["engine"], s["task"], s["images"], s["errors"], agree, speed]
        )

    detail_headers = ["image", "task", "engine", "result"]
    detail_rows = []
    for r in rows:
        if "error" in r:
            result = f"ERROR {r['error']}"
        elif r["task"] == "clip":
            result = f"cosine {r['cosine']:.6f}"
        elif r["task"] == "faces":
            result = (
                f"{r['count']} vs {r['ref_count']} faces, "
                f"{r['matched']} matched, IoU mean {r['mean_iou']:.3f}"
            )
        else:
            result = f"{r['count']} vs {r['ref_count']} strings, {r['shared']} shared"
            if r["only_ref"]:
                result += f" | missed: {r['only_ref']}"
            if r["only_other"]:
                result += f" | extra: {r['only_other']}"
        detail_rows.append([r["image"], r["task"], r["engine"], result])

    engine_list = ", ".join(f"{n} ({u})" for n, u in engines.items())
    page = f"""<!doctype html><meta charset="utf-8">
<title>ML parity</title>
<style>
 body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 2rem auto;
        max-width: 70rem; padding: 0 1rem; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; }}
 th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #ddd;
          vertical-align: top; }}
 th {{ font-weight: 600; border-bottom: 2px solid #bbb; }}
 td:nth-child(4) {{ font-variant-numeric: tabular-nums; }}
 p.sub {{ color: #666; }}
</style>
<h1>ML parity</h1>
<p class="sub">{esc(len(images))} images, reference <b>{esc(reference)}</b>.
Engines: {esc(engine_list)}.<br>
Models: CLIP {esc(CLIP_MODEL)}, faces {esc(FACE_MODEL)}, OCR {esc(OCR_MODEL)}.</p>
<p class="sub">Agreement is not correctness. If two engines are wrong the same
way this reports parity.</p>
<h2>Summary</h2>
{table(sum_headers, sum_rows)}
<h2>Every image</h2>
{table(detail_headers, detail_rows)}
"""
    path.write_text(page)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare ML engines on the same images.")
    ap.add_argument(
        "--reference",
        required=True,
        metavar="NAME=URL",
        help="the engine everything else is measured against, "
        "normally Immich's own container",
    )
    ap.add_argument(
        "--engine",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="an engine to compare (repeatable)",
    )
    ap.add_argument(
        "--images",
        nargs="+",
        required=True,
        help="image files, or directories to take images from",
    )
    ap.add_argument(
        "--tasks",
        nargs="+",
        default=["clip", "faces", "ocr"],
        choices=["clip", "faces", "ocr"],
    )
    ap.add_argument("--out", default="ml-parity.html")
    ap.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="per request; the first call to a cold engine " "downloads models",
    )
    args = ap.parse_args()

    def split(spec):
        if "=" not in spec:
            ap.error(f"expected NAME=URL, got {spec!r}")
        name, url = spec.split("=", 1)
        return name, url.rstrip("/")

    ref_name, ref_url = split(args.reference)
    engines = {ref_name: ref_url}
    for spec in args.engine:
        name, url = split(spec)
        engines[name] = url
    if len(engines) < 2:
        ap.error("give at least one --engine besides the reference")

    images = []
    for entry in args.images:
        p = pathlib.Path(entry).expanduser()
        if p.is_dir():
            images += sorted(
                q
                for q in p.iterdir()
                if q.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            )
        elif p.is_file():
            images.append(p)
    if not images:
        print("no images found", file=sys.stderr)
        return 2

    print(
        f"[parity] {len(images)} images x {len(args.tasks)} tasks "
        f"x {len(engines)} engines",
        flush=True,
    )
    rows, timings = run(images, engines, ref_name, args.tasks, args.timeout)
    summary = summarise(rows, timings, ref_name)

    for s in summary:
        bits = [f"{s['engine']:>12s} {s['task']:6s}"]
        if s["task"] == "clip" and "mean_cosine" in s:
            bits.append(
                f"cosine mean {s['mean_cosine']:.5f} " f"worst {s['worst_cosine']:.5f}"
            )
        elif s["task"] == "faces" and "mean_iou" in s:
            bits.append(
                f"count match {s['count_matches']}/{s['images']} "
                f"IoU mean {s['mean_iou']:.3f} worst {s['worst_iou']:.3f}"
            )
        elif s["task"] == "ocr" and "recall" in s:
            bits.append(
                f"identical {s['exact_sets']}/{s['images']} "
                f"recall {s['recall']:.3f}"
            )
        if s["errors"]:
            bits.append(f"ERRORS {s['errors']}")
        if "median_s" in s:
            bits.append(f"{s['median_s']:.2f}s")
        print("  " + "  ".join(bits), flush=True)

    out = write_report(pathlib.Path(args.out), rows, summary, engines, ref_name, images)
    print(f"[parity] wrote {out}", flush=True)
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
