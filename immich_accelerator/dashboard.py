"""Immich Accelerator Dashboard — real-time monitoring web UI.

A lightweight FastAPI server that exposes the accelerator's status as
both API endpoints and a beautiful single-page dashboard. Polls the
Immich database, checks service health, and reads system metrics.

Usage:
    python -m immich_accelerator dashboard          # http://localhost:8422
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

    has_psql = os.path.exists(psql)

    # Try direct psql connection (remote setups, or local with password)
    if has_psql and (password or host != "localhost"):
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
        # Don't return — fall through to docker exec fallback

    # Fallback: docker exec (local setups, or psql failed above)
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
            _db_error_logged = False
            return result

    # Nothing worked — log once
    if not _db_error_logged:
        if not has_psql:
            log.warning("Dashboard: psql not found. Install with: brew install libpq")
        elif host != "localhost":
            log.warning("Dashboard: cannot reach Postgres at %s:%s", host, port)
            log.warning(
                "  Check that the port is exposed (not 127.0.0.1) and reachable from this Mac"
            )
        else:
            log.warning(
                "Dashboard: cannot connect to Postgres. Check that Docker is running."
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
    worker_rss_mb = 0
    pid_file = Path.home() / ".immich-accelerator" / "pids" / "worker.pid"
    try:
        if pid_file.exists():
            pid = int(pid_file.read_text().strip().split("\n")[0])
            os.kill(pid, 0)  # check if process exists
            worker_alive = True
            # Grab RSS for memory-growth detection. On macOS `ps -o rss=`
            # returns kilobytes. Rising RSS over hours suggests a libvips
            # or Sharp memory leak causing the thumbnail slowdown (#33).
            rss_out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "rss="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if rss_out.returncode == 0 and rss_out.stdout.strip():
                worker_rss_mb = round(int(rss_out.stdout.strip()) / 1024)
    except (ValueError, OSError, subprocess.SubprocessError):
        pass

    # Processing counts — each queue counted over the SAME population Immich
    # itself uses (see asset-job.repository.js), so our numbers match Immich's
    # Jobs page. Two denominators: live non-hidden assets (thumbnails, OCR,
    # video) and "assets with previews" (CLIP + faces — Immich won't queue
    # those until a preview exists). Counting side tables (smart_search,
    # asset_job_status) unfiltered overcounts, since rows persist for
    # deleted/hidden assets — that's how completion read >100% (#68).
    #   live = asset, deletedAt IS NULL, visibility != hidden
    #   awp  = live + has an asset_job_status row + has a Preview file
    counts_raw = _query_db(
        "WITH live AS ("
        "  SELECT id, type, thumbhash FROM asset"
        "  WHERE \"deletedAt\" IS NULL AND visibility != 'hidden'), "
        "awp AS ("
        '  SELECT a.id, js."facesRecognizedAt" FROM asset a'
        '  JOIN asset_job_status js ON js."assetId" = a.id'
        "  WHERE a.\"deletedAt\" IS NULL AND a.visibility != 'hidden'"
        "    AND EXISTS (SELECT 1 FROM asset_file f"
        "               WHERE f.\"assetId\" = a.id AND f.type = 'preview')) "
        "SELECT (SELECT COUNT(*) FROM live), "
        "(SELECT COUNT(*) FROM awp), "
        "(SELECT COUNT(*) FROM live WHERE type = 'VIDEO'), "
        "(SELECT COUNT(*) FROM live WHERE thumbhash IS NOT NULL), "
        '(SELECT COUNT(*) FROM awp WHERE EXISTS (SELECT 1 FROM smart_search s WHERE s."assetId" = awp.id)), '
        '(SELECT COUNT(*) FROM awp WHERE "facesRecognizedAt" IS NOT NULL), '
        '(SELECT COUNT(*) FROM live l WHERE EXISTS (SELECT 1 FROM asset_job_status js WHERE js."assetId" = l.id AND js."ocrAt" IS NOT NULL)), '
        "(SELECT COUNT(*) FROM live l WHERE l.type = 'VIDEO' AND EXISTS (SELECT 1 FROM asset_file f WHERE f.\"assetId\" = l.id AND f.type = 'encoded_video'))",
        config,
    )

    # total_assets: thumbnails/OCR denominator; total_previews: CLIP/faces.
    total_assets = total_previews = total_videos = 0
    thumbs = clip = faces = ocr = encoded_videos = 0
    if counts_raw and "|" in counts_raw:
        parts = counts_raw.split("|")
        if len(parts) == 8:
            try:
                (
                    total_assets,
                    total_previews,
                    total_videos,
                    thumbs,
                    clip,
                    faces,
                    ocr,
                    encoded_videos,
                ) = [int(p) for p in parts]
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

    # Per-queue activity from Immich jobs API. Also capture the raw
    # active + waiting counts so the frontend can show "X remaining"
    # (matching what the Immich admin panel shows) instead of only
    # displaying DB-derived done/total which measures a different thing.
    queue_status = {}
    queue_counts = {}
    api_key = config.get("api_key", "")
    immich_url = config.get("immich_url", "http://localhost:2283")
    jobs_api_error = ""
    if api_key:
        import urllib.request as _urlreq2

        try:
            req = _urlreq2.Request(
                f"{immich_url}/api/jobs", headers={"x-api-key": api_key}
            )
            with _urlreq2.urlopen(req, timeout=5) as r:
                body = r.read()
                if not body or not body.strip():
                    raise ValueError(f"empty response from {immich_url}/api/jobs")
                jobs = json.loads(body)
                queue_map = {
                    "thumbnailGeneration": "thumbnails",
                    "smartSearch": "clip",
                    "faceDetection": "faces",
                    "ocr": "ocr",
                    "videoConversion": "video",
                }
                for immich_name, our_name in queue_map.items():
                    counts = jobs.get(immich_name, {}).get("jobCounts", {})
                    active = counts.get("active", 0)
                    waiting = counts.get("waiting", 0)
                    queue_status[our_name] = (active + waiting) > 0
                    queue_counts[our_name] = active + waiting
        except Exception as e:
            err = str(e)
            # Make common errors human-readable
            if "Expecting value" in err or "empty response" in err:
                jobs_api_error = (
                    f"Immich API returned empty response (check immich_url in config)"
                )
            elif "401" in err or "403" in err:
                jobs_api_error = "API key rejected (check api_key in config)"
            elif "Connection refused" in err or "ECONNREFUSED" in err:
                jobs_api_error = f"cannot reach {immich_url} (is Immich running?)"
            elif "timed out" in err.lower():
                jobs_api_error = "Immich API timed out (server under heavy load?)"
            else:
                jobs_api_error = err[:200]
            log.warning("jobs API unreachable: %s", jobs_api_error)
    else:
        jobs_api_error = "no api_key configured"

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
        # Clamp to 100: done can briefly exceed tot from count skew between the
        # asset-total and per-stage-done queries (assets added/removed mid-scan),
        # and a completion percentage can't exceed 100% (#68).
        return {
            "done": done,
            "total": tot,
            "pct": min(round(done / max(tot, 1) * 100, 1), 100.0),
            "skipped": 0,
        }

    # Video transcode: use queue state for pct when active, 100% when idle + transcoded
    vid_active = queue_status.get("video", False)
    if vid_active and total_videos > 0:
        vid_pct = min(round(encoded_videos / total_videos * 100, 1), 100.0)
    elif encoded_videos > 0:
        vid_pct = 100.0
    else:
        vid_pct = 0

    status = {
        "services": {
            "worker": {
                "alive": worker_alive,
                "name": "Microservices Worker",
                "rss_mb": worker_rss_mb,
            },
            "ml": {"alive": ml_alive, "name": "ML Service"},
            "docker": {"alive": total_assets > 0, "name": "Docker (API)"},
        },
        "progress": {
            # Each bar uses Immich's own denominator: thumbnails/OCR over all
            # live assets, CLIP/faces over assets-with-previews.
            "thumbnails": prog(thumbs, total_assets),
            "clip": prog(clip, total_previews),
            "faces": prog(faces, total_previews),
            "ocr": prog(ocr, total_assets),
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
        "queue_counts": queue_counts,
        "jobs_api_error": jobs_api_error,
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
