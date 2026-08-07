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

CONFIG_FILE = Path.home() / ".immich-accelerator" / "config.json"

# Cache to avoid hammering the DB on every request
_cache: dict = {}
_cache_ts: float = 0
_CACHE_TTL = 3  # seconds

_static_hw: dict | None = None


def _component_on(config: dict, name: str) -> bool:
    """Whether a component ("worker", "ml", "dashboard") is switched on.

    Deliberately a small duplicate of __main__._component_enabled rather than an
    import: the dashboard is spawned as `python -m immich_accelerator dashboard`,
    so __main__ is the running module, and importing it from here would load a
    second copy of it with its own state. Keep the two in sync.
    """
    if name in config:
        return bool(config[name])
    if config.get("ml_only"):  # legacy preset: worker off, everything else on
        return name != "worker"
    return True


def _worker_enabled(config: dict) -> bool:
    """Whether the worker component is switched on."""
    return _component_on(config, "worker")


def _has_database(config: dict) -> bool:
    """Whether this install has an Immich database to report on.

    A config shape question, not a component question. `setup --ml-only` writes
    no db fields at all; every other setup path writes db_hostname. Keeping this
    separate from the worker switch matters, because a full install with the
    worker merely turned off still owns its library and its progress bars must
    keep working."""
    return bool(config.get("db_hostname"))


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


def _reload_config(config: dict) -> dict:
    """Re-read config.json, falling back to the caller's copy.

    create_app captures the config once at process start, so a component toggled
    while the dashboard is running would otherwise never reach it and the page
    would keep drawing a service switched off minutes ago.

    Called from the request handlers rather than from get_status, so get_status
    stays a pure function of the config it is handed: making it silently prefer
    a file over its own argument would change the contract for every caller and
    every test, and would behave differently on a machine that happens to have a
    real install. A partially-written file just means one cycle with the
    previous copy.
    """
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return config


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
    #
    # Skipped only when this box has no database to ask, which is a statement
    # about config shape, not about the worker switch. A node set up by
    # `setup --ml-only` has no db credentials at all, and querying would spawn a
    # doomed psql/docker-exec call every uncached poll and log a misleading
    # "cannot connect to Postgres". But a full install with the worker merely
    # switched off still owns its library, so its progress bars must keep
    # working: gating those on the worker would zero them out for the exact
    # user watching to see whether turning the worker off was a good idea.
    counts_raw = (
        ""
        if not _has_database(config)
        else _query_db(
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

    # Whether we actually reached the jobs API this cycle. An empty
    # queue_status means unreachable, not idle, and the two must never be
    # confused: one is "nothing is scheduled", the other is "we have no idea".
    queues_known = bool(queue_status)

    def prog(queue, done, tot):
        """One stage's completion, reported honestly.

        This used to return a flat 100% whenever Immich's queues were idle and
        call the remainder "skipped", on the theory that anything still undone
        after a drained queue must be unprocessable. That theory holds for a
        handful of corrupt files and collapses for a library that simply has
        not been queued yet: on a 174k-asset library with 106k assets missing
        embeddings it drew four full bars and reported everything complete.

        Idle is not finished. The percentage is now always done/total, and the
        nuance moves to "unqueued", which is literally true in both cases:
        that work exists, nothing is running it, and nothing will until
        someone queues it (the dashboard's Re-queue Missing button, or
        Immich's own Jobs page).

        "unqueued" is judged per queue, not globally. Asking "is ANY queue
        busy" would blank the hint on every bar the moment one unrelated queue
        picked up work, so a video transcode backlog, or the first seconds
        after Re-queue Missing, would hide "106,220 not queued" from the CLIP
        bar precisely while someone was watching it, then flash it back when
        the last queue drained. queue_status already answers this per queue.
        """
        return {
            "done": done,
            "total": tot,
            # Clamp to 100: done can briefly exceed tot from count skew between
            # the asset-total and per-stage-done queries (assets added/removed
            # mid-scan), and a completion percentage can't exceed 100% (#68).
            "pct": min(round(done / max(tot, 1) * 100, 1), 100.0),
            "unqueued": (
                max(tot - done, 0)
                if (queues_known and not queue_status.get(queue, False))
                else 0
            ),
        }

    # A component the user switched off is omitted, not drawn dead. Same rule as
    # the menu bar: "off because I said so" must not render as a red dot, or the
    # dots stop meaning anything. The page renders whatever keys arrive, so
    # leaving one out is the whole fix.
    services = {}
    if _worker_enabled(config):
        services["worker"] = {
            "alive": worker_alive,
            "name": "Microservices Worker",
            "rss_mb": worker_rss_mb,
        }
    if _component_on(config, "ml"):
        services["ml"] = {"alive": ml_alive, "name": "ML Service"}
    if _has_database(config):
        # Derived from the asset count, so it only means anything where there is
        # a library to count. Tied to the database, not to the worker switch: an
        # install with the worker off still has both.
        services["docker"] = {"alive": total_assets > 0, "name": "Docker (API)"}

    status = {
        "services": services,
        "progress": {
            # Each bar uses Immich's own denominator: thumbnails/OCR over all
            # live assets, CLIP/faces over assets-with-previews.
            "thumbnails": prog("thumbnails", thumbs, total_assets),
            "clip": prog("clip", clip, total_previews),
            "faces": prog("faces", faces, total_previews),
            "ocr": prog("ocr", ocr, total_assets),
            # Video went through the same "idle means done" special case and
            # gets the same honest treatment. Videos Immich's transcode policy
            # never selects show up as unqueued, which is exactly what they
            # are: no encoded copy, and nothing scheduled to make one.
            "video": prog("video", encoded_videos, total_videos),
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

    # The captured config is the fallback; every request re-reads the file, so a
    # component toggle or an added api_key takes effect without a restart.
    # Both handlers do it: a reload in only one of them is the same bug with a
    # smaller blast radius.
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _load_html()

    @app.get("/api/status")
    async def api_status():
        return JSONResponse(get_status(_reload_config(config)))

    @app.post("/api/requeue")
    async def api_requeue():
        """Trigger 'Run All Missing' for thumbnail, CLIP, faces, and OCR queues."""
        import urllib.request, urllib.error

        live = _reload_config(config)
        api_key = live.get("api_key", "")
        immich_url = live.get("immich_url", "http://localhost:2283")
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
