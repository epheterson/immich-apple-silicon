"""Immich Accelerator Dashboard — real-time monitoring web UI.

A lightweight FastAPI server that exposes the accelerator's status as
both API endpoints and a beautiful single-page dashboard. Polls the
Immich database, checks service health, and reads system metrics.

Usage:
    python -m immich_accelerator dashboard          # http://localhost:8420
    python -m immich_accelerator dashboard --port 9000

Security note: The dashboard renders data from the local Immich database
and system metrics. All data sources are trusted (localhost only). The
HTML rendering uses template literals with numeric/string data from our
own API — no user-supplied content is rendered as HTML.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger("dashboard")

# Cache to avoid hammering the DB on every request
_cache: dict = {}
_cache_ts: float = 0
_CACHE_TTL = 3  # seconds

_static_hw: dict | None = None


def _get_accelerator_version() -> str:
    """Get accelerator version from the VERSION file or fall back."""
    try:
        version_file = Path(__file__).parent.parent / "VERSION"
        if version_file.exists():
            return version_file.read_text().strip()
    except OSError:
        pass
    return "1.0.0"


def _run(cmd: list[str], timeout: int = 5, env: dict | None = None) -> str:
    """Run a command and return stdout, or empty string on failure."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


_db_error_logged = False


def _query_db(sql: str, config: dict) -> str:
    """Run a SQL query against Immich's Postgres.

    Uses direct psql connection when DB host/password are configured (remote
    setups). Falls back to docker exec for local setups (backwards compat).
    """
    global _db_error_logged
    host = config.get("db_hostname", "localhost")
    port = config.get("db_port", "5432")
    user = config.get("db_username", "postgres")
    password = config.get("db_password", "")
    db = config.get("db_name", "immich")

    # Direct psql connection — works for both local and remote setups
    psql = "/opt/homebrew/opt/libpq/bin/psql"
    if not os.path.exists(psql):
        psql = "/opt/homebrew/bin/psql"
    if not os.path.exists(psql):
        psql = "/usr/local/bin/psql"

    if os.path.exists(psql) and (password or host != "localhost"):
        env = {**os.environ}
        if password:
            env["PGPASSWORD"] = password
        result = _run(
            [psql, "-h", host, "-p", port, "-U", user, "-d", db, "-t", "-A", "-c", sql],
            env=env,
        )
        if result:
            _db_error_logged = False
            return result
        if not _db_error_logged:
            log.warning("Dashboard: cannot reach Postgres at %s:%s", host, port)
            log.warning(
                "  Check that the port is exposed (not bound to 127.0.0.1) and reachable from this Mac"
            )
            _db_error_logged = True
        return ""

    # Fallback: docker exec (local setups without psql installed)
    docker = "/usr/local/bin/docker"
    if not os.path.exists(docker):
        docker = "/opt/homebrew/bin/docker"
    if os.path.exists(docker):
        container = config.get("db_container", "immich_postgres")
        result = _run(
            [
                docker,
                "exec",
                container,
                "psql",
                "-U",
                user,
                "-d",
                db,
                "-t",
                "-A",
                "-c",
                sql,
            ]
        )
        if result:
            return result

    # Nothing worked — log once
    if not _db_error_logged:
        if not os.path.exists(psql.rsplit("/", 1)[0] + "/psql"):
            log.warning("Dashboard: psql not found. Install with: brew install libpq")
        else:
            log.warning(
                "Dashboard: cannot connect to Postgres. Check config and port exposure."
            )
        _db_error_logged = True
    return ""


def get_status(config: dict) -> dict:
    """Get full accelerator status. Cached for _CACHE_TTL seconds."""
    global _cache, _cache_ts

    now = time.monotonic()
    if now - _cache_ts < _CACHE_TTL and _cache:
        return _cache

    # Service health
    import urllib.request as _urlreq

    ml_alive = False
    try:
        with _urlreq.urlopen("http://localhost:3003/ping", timeout=2) as r:
            ml_alive = r.read().decode().strip() == "pong"
    except Exception:
        pass

    # Check worker PID file (more reliable than pgrep — process name is 'node', not 'immich')
    worker_alive = False
    pid_file = Path.home() / ".immich-accelerator" / "pids" / "worker.pid"
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip().split("\n")[0])
            os.kill(pid, 0)  # check if process exists
            worker_alive = True
    except (ValueError, OSError):
        pass

    # Processing counts
    # Exclude hidden assets (Live Photo motion files) — Immich skips them too
    counts_raw = _query_db(
        "SELECT COUNT(*) FILTER (WHERE thumbhash IS NOT NULL), COUNT(*), "
        "(SELECT COUNT(*) FROM smart_search), "
        '(SELECT COUNT(*) FROM asset_job_status WHERE "facesRecognizedAt" IS NOT NULL), '
        '(SELECT COUNT(*) FROM asset_job_status WHERE "ocrAt" IS NOT NULL), '
        "COUNT(*) FILTER (WHERE type = 'VIDEO' AND visibility != 'hidden'), "
        "(SELECT COUNT(*) FROM asset_file af JOIN asset a ON a.id = af.\"assetId\" WHERE af.type = 'encoded_video' AND a.visibility != 'hidden') "
        "FROM asset WHERE \"deletedAt\" IS NULL AND visibility != 'hidden'",
        config,
    )

    thumbs, total, clip, faces, ocr, total_videos, encoded_videos = 0, 0, 0, 0, 0, 0, 0
    if counts_raw and "|" in counts_raw:
        parts = counts_raw.split("|")
        if len(parts) == 7:
            try:
                thumbs, total, clip, faces, ocr, total_videos, encoded_videos = [
                    int(p) for p in parts
                ]
            except ValueError:
                pass

    # System metrics
    load_raw = _run(["sysctl", "-n", "vm.loadavg"])
    load_1m = 0.0
    if load_raw:
        try:
            load_1m = float(load_raw.strip("{ }").split()[0])
        except (ValueError, IndexError):
            pass

    # Static hardware info (never changes, cached on first call)
    global _static_hw
    if _static_hw is None:
        mem_raw = _run(["sysctl", "-n", "hw.memsize"])
        cpu_raw = _run(["sysctl", "-n", "hw.ncpu"])
        _static_hw = {
            "mem_total_gb": round(int(mem_raw) / (1024**3), 1) if mem_raw else 0,
            "cpus": int(cpu_raw) if cpu_raw else 0,
        }

    # Per-queue activity from Immich jobs API
    queue_status = {}
    api_key = config.get("api_key", "")
    immich_url = config.get("immich_url", "http://localhost:2283")
    if api_key:
        import urllib.request as _urlreq2

        try:
            req = _urlreq2.Request(
                f"{immich_url}/api/jobs", headers={"x-api-key": api_key}
            )
            with _urlreq2.urlopen(req, timeout=2) as r:
                jobs = json.loads(r.read())
                queue_map = {
                    "thumbnailGeneration": "thumbnails",
                    "smartSearch": "clip",
                    "faceDetection": "faces",
                    "ocr": "ocr",
                    "videoConversion": "video",
                }
                for immich_name, our_name in queue_map.items():
                    counts = jobs.get(immich_name, {}).get("jobCounts", {})
                    queue_status[our_name] = (
                        counts.get("active", 0) + counts.get("waiting", 0) > 0
                    )
        except Exception:
            pass

    # Versions
    version = config.get("version", "?")

    # When all queues are confirmed idle (API responded, nothing active),
    # unprocessable assets are "skipped." Only apply when we actually got
    # queue data — empty queue_status means API unreachable, not "idle."
    queues_known = bool(queue_status)
    any_active = queues_known and any(queue_status.values())

    def prog(done, tot):
        if queues_known and not any_active and done < tot:
            return {"done": done, "total": tot, "pct": 100.0, "skipped": tot - done}
        return {
            "done": done,
            "total": tot,
            "pct": round(done / max(tot, 1) * 100, 1),
            "skipped": 0,
        }

    # Video transcode: use queue state for pct when active, 100% when idle + transcoded
    vid_active = queue_status.get("video", False)
    if vid_active and total_videos > 0:
        vid_pct = round(encoded_videos / total_videos * 100, 1)
    elif encoded_videos > 0:
        vid_pct = 100.0
    else:
        vid_pct = 0

    status = {
        "services": {
            "worker": {"alive": worker_alive, "name": "Microservices Worker"},
            "ml": {"alive": ml_alive, "name": "ML Service"},
            "docker": {"alive": total > 0, "name": "Docker (API)"},
        },
        "progress": {
            "thumbnails": prog(thumbs, total),
            "clip": prog(clip, total),
            "faces": prog(faces, total),
            "ocr": prog(ocr, total),
            "video": {
                "done": encoded_videos,
                "total": total_videos,
                "pct": vid_pct,
                "skipped": 0,
            },
        },
        "system": {
            "load_1m": load_1m,
            "mem_total_gb": _static_hw["mem_total_gb"],
            "cpus": _static_hw["cpus"],
        },
        "version": version,
        "accelerator_version": _get_accelerator_version(),
        "queue_active": queue_status,
    }

    _cache = status
    _cache_ts = now
    return status


def _load_html() -> str:
    """Load the dashboard HTML from the static file."""
    html_path = Path(__file__).parent / "dashboard.html"
    return html_path.read_text()


def create_app(config: dict):
    """Create the FastAPI dashboard app."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="Immich Accelerator Dashboard")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _load_html()

    @app.get("/api/status")
    async def api_status():
        return JSONResponse(get_status(config))

    @app.post("/api/requeue")
    async def api_requeue():
        """Trigger 'Run All Missing' for thumbnail, CLIP, faces, and OCR queues."""
        import urllib.request, urllib.error

        api_key = config.get("api_key", "")
        immich_url = config.get("immich_url", "http://localhost:2283")
        if not api_key:
            return JSONResponse({"error": "No API key configured"}, status_code=400)

        results = {}
        for queue in [
            "thumbnailGeneration",
            "smartSearch",
            "faceDetection",
            "ocr",
            "videoConversion",
        ]:
            try:
                data = b'{"command": "start", "force": false}'
                req = urllib.request.Request(
                    f"{immich_url}/api/jobs/{queue}",
                    data=data,
                    method="PUT",
                    headers={
                        "x-api-key": api_key,
                        "Content-Type": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    results[queue] = "ok"
            except urllib.error.HTTPError as e:
                # 400 "already running" is fine — job was already queued
                results[queue] = "ok" if e.code == 400 else "failed"
            except Exception:
                results[queue] = "failed"

        return JSONResponse(results)

    return app


def run_dashboard(config: dict, port: int = 8420):
    """Start the dashboard server."""
    import uvicorn

    app = create_app(config)
    log.info("Dashboard: http://localhost:%d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
