"""Immich's /predict multipart wire format, in one place.

Three scripts here talk to an ML service the same way, and each had grown its
own copy of this: ml-parity.py, ml-preflight.py and native-ml-preflight.py.
They had already drifted apart in boundary string, filename and which parts
they knew how to send, which is the usual cost of four copies. The knowledge
of which nesting the service actually reads lived in exactly one of them.

Deliberately a plain module in scripts/ rather than part of the package. Two
of these scripts are the mandatory Apple Silicon gates and are run directly,
sometimes under a different interpreter than the one the package is installed
for, so they cannot import immich_accelerator. A sibling module they can all
import is the most sharing available without breaking that.

The product's own copy in immich_accelerator/__main__.py stays where it is,
for the same reason.
"""

from __future__ import annotations

import json
import urllib.request

BOUNDARY = "----immich-accelerator"


def multipart(
    entries: dict,
    *,
    image: bytes | None = None,
    text: str | None = None,
    filename: str = "t.jpg",
) -> tuple[bytes, str]:
    """Build the body and Content-Type for one /predict call.

    Immich sends `entries` as a JSON form field plus an optional image part,
    and the service also accepts a text part for CLIP text embedding. Both
    optional parts are omitted entirely when not supplied, rather than sent
    empty, because an empty part is not the same request.
    """
    parts = [
        f"--{BOUNDARY}\r\n".encode(),
        b'Content-Disposition: form-data; name="entries"\r\n\r\n',
        json.dumps(entries).encode() + b"\r\n",
    ]
    if image is not None:
        parts += [
            f"--{BOUNDARY}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            "Content-Type: image/jpeg\r\n\r\n".encode(),
            image,
            b"\r\n",
        ]
    if text is not None:
        parts += [
            f"--{BOUNDARY}\r\n".encode(),
            b'Content-Disposition: form-data; name="text"\r\n\r\n',
            text.encode() + b"\r\n",
        ]
    parts.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={BOUNDARY}"


def predict(
    base: str,
    entries: dict,
    *,
    image: bytes | None = None,
    text: str | None = None,
    timeout: int = 60,
    filename: str = "t.jpg",
) -> dict:
    """POST one /predict call and return the decoded response.

    Raises rather than returning a sentinel on a transport failure: every
    caller here is a gate or a measurement, and a call that did not happen
    must not be mistaken for one that returned nothing.
    """
    body, content_type = multipart(entries, image=image, text=text, filename=filename)
    req = urllib.request.Request(
        f"{base}/predict",
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())
