"""Logic shared by the CLI and the dashboard.

This module exists because of how the dashboard is started. It runs as
``python -m immich_accelerator dashboard``, which makes ``__main__`` the
running module, so ``dashboard.py`` cannot import from it: doing so loads a
second copy of an 8,000-line module with its own separate state. The
dashboard's answer was to keep small hand-written duplicates and a note
saying "keep the two in sync", with nothing enforcing that.

A plain module has no such problem. Both sides import this one and get the
same object, so there is one implementation rather than two that agree by
convention. Anything both the CLI and the dashboard need to know belongs
here rather than in either of them.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
import subprocess

log = logging.getLogger("accelerator")

COMPONENTS = ("worker", "ml", "dashboard")

# Nothing here should be able to hang forever waiting on another program.
# Every call through the helpers below gets a timeout, and this is the default
# for the ones that do not name their own: long enough for a local tool to
# answer under load, short enough that a wedged one is noticed.
DEFAULT_TIMEOUT = 30


def component_enabled(name: str, config: dict) -> bool:
    """Whether a component should be running. Defaults True.

    Absent means enabled, because every config written before these keys
    existed has none of them, and an upgrade must not silently turn anything
    off.

    An explicit component key beats the legacy "ml_only" preset. That order
    matters: without it, a user who ran `setup --ml-only` once could never turn
    the worker back on without hand-editing config.json.
    """
    if name in config:
        return bool(config[name])
    if config.get("ml_only"):  # preset: worker off, everything else on
        return name != "worker"
    return True


def run_output(
    cmd: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Run *cmd* and return its stdout, or None if it did not succeed.

    Replaces the shape written out by hand dozens of times across this
    project: run with capture_output and text, catch OSError and
    TimeoutExpired, check returncode, then use stdout. It is not a wrapper
    around subprocess.run with the same surface, which would be indirection
    rather than simplification; it replaces the whole pattern, error handling
    included.

    None rather than "" on failure, deliberately. An empty string is exactly
    what a command that succeeded and printed nothing returns, so collapsing
    the two makes "we could not ask" indistinguishable from "the answer is
    nothing". Several bugs shipped from here have had that shape. A caller
    that genuinely does not care can write ``or ""``.

    Only for commands where a non-zero exit means failure. Tools whose exit
    code is an *answer* rather than an error (lsof, pgrep and grep all exit 1
    for "found nothing") must keep calling subprocess.run directly, or this
    would silently discard the output they did produce.
    """
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log.debug("timed out after %ss: %s", timeout, " ".join(map(str, cmd)))
        return None
    except OSError as e:
        log.debug("could not run %s: %s", " ".join(map(str, cmd)), e)
        return None
    if r.returncode != 0:
        log.debug(
            "exit %s from %s: %s",
            r.returncode,
            " ".join(map(str, cmd)),
            (r.stderr or "").strip()[:200],
        )
        return None
    return r.stdout


def run_ok(
    cmd: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    input: str | None = None,
) -> bool:
    """Run *cmd* for its effect and say whether it worked.

    For the calls that capture output purely to keep a command quiet and then
    look only at the exit status, or at nothing at all. Output is still
    captured, for the same reason those calls captured it, and a failure
    leaves its stderr in the debug log via run_output rather than vanishing.

    Deliberately a thin shim rather than its own copy of the same try/except:
    the two were byte-identical apart from the return value.
    """
    return run_output(cmd, timeout=timeout, input=input) is not None


# Immich's "missing" runs, in pipeline order: each stage only picks up assets
# the one before has finished, and asking for "missing" (force false) queues
# nothing for an asset that already has the result. Metadata leads because an
# asset that never had it never gets a thumbnail either.
MISSING_QUEUES = (
    "metadataExtraction",
    "thumbnailGeneration",
    "smartSearch",
    "faceDetection",
    "ocr",
    "videoConversion",
)

# How often the watcher queues missing work on its own, when it is switched on
# (auto_requeue in config.json). Daily: a gap that formed overnight is closed
# by the next evening, and an idle Immich asked once a day costs nothing.
AUTO_REQUEUE_EVERY = 24 * 3600


def _immich(url: str, api_key: str, path: str, body: bytes | None = None, timeout: float = 10):
    req = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=body,
        method="PUT" if body is not None else "GET",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def immich_busy(url: str, api_key: str) -> bool | None:
    """Is any Immich queue running or holding work? None when it cannot be
    asked, which callers must treat as "do not touch it"."""
    try:
        jobs = _immich(url, api_key, "/api/jobs", timeout=5)
    except Exception:
        return None
    return any(
        (q.get("jobCounts", {}).get("active", 0) + q.get("jobCounts", {}).get("waiting", 0)) > 0
        for q in jobs.values()
        if isinstance(q, dict)
    )


def requeue_missing(url: str, api_key: str) -> dict:
    """Ask Immich to queue whatever it has not done yet, stage by stage, the
    same as each queue's "Missing" button on its Jobs page."""
    results = {}
    for queue in MISSING_QUEUES:
        try:
            _immich(url, api_key, f"/api/jobs/{queue}", b'{"command": "start", "force": false}')
            results[queue] = "ok"
        except urllib.error.HTTPError as e:
            # 400 is "already running": it is queued, which is what we asked.
            results[queue] = "ok" if e.code == 400 else "failed"
        except Exception:
            results[queue] = "failed"
    return results


def set_config_value(config_file: Path, key: str, value) -> dict:
    """Change one key in config.json and write it back the way save_config
    does: whole file, atomically, owner-only (it holds the database password).
    Returns the config as written."""
    config = json.loads(config_file.read_text())
    config[key] = value
    tmp = config_file.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    tmp.rename(config_file)
    return config
