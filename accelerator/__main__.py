"""Immich Accelerator — run Immich microservices natively on macOS.

Usage:
    python -m accelerator setup     # detect Immich, checkout code, configure
    python -m accelerator start     # start native worker + ML service
    python -m accelerator stop      # stop native services
    python -m accelerator status    # show what's running
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

def _read_version() -> str:
    """Read version from VERSION file (single source of truth)."""
    try:
        return (Path(__file__).parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "1.0.0"

__version__ = _read_version()

log = logging.getLogger("accelerator")

DATA_DIR = Path.home() / ".immich-accelerator"
CONFIG_FILE = DATA_DIR / "config.json"
PID_DIR = DATA_DIR / "pids"
LOG_DIR = DATA_DIR / "logs"


# --- Utility ---

def find_binary(name: str, paths: list[str], install_hint: str) -> str:
    for p in paths:
        if os.path.isfile(p):
            return p
    raise RuntimeError(f"{name} not found. {install_hint}")


def find_docker() -> str:
    return find_binary("Docker", ["/usr/local/bin/docker", "/opt/homebrew/bin/docker"],
                       "Install Docker Desktop or OrbStack.")


def find_node() -> str:
    return find_binary("Node.js", ["/opt/homebrew/bin/node", "/usr/local/bin/node"],
                       "Install with: brew install node")


def find_npm() -> str:
    return find_binary("npm", ["/opt/homebrew/bin/npm", "/usr/local/bin/npm"],
                       "Install with: brew install node")


def check_port(host: str, port: int, label: str) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        log.error("%s not reachable at %s:%d", label, host, port)
        return False


def is_valid_version(version: str) -> bool:
    """Check if version looks like a semver (with or without v prefix)."""
    return bool(re.match(r"^v?\d+\.\d+\.\d+", version))


# --- Docker detection ---

def detect_immich(docker: str) -> dict:
    """Detect running Immich instance from Docker."""
    result = subprocess.run(
        [docker, "ps", "--format", "{{.Names}}\t{{.Image}}"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Docker not running or not accessible: {result.stderr.strip()}")

    server_container = None
    for line in result.stdout.strip().split("\n"):
        if not line or "\t" not in line:
            continue
        name, image = line.split("\t", 1)
        if "immich" in image.lower() and "server" in image.lower():
            server_container = name
            break
        if "immich" in name.lower() and "server" in name.lower():
            server_container = name
            break

    if not server_container:
        raise RuntimeError("No Immich server container found. Is Immich running in Docker?")

    # Get version from package.json inside the container
    version = "unknown"
    version_result = subprocess.run(
        [docker, "exec", server_container, "cat", "/usr/src/app/server/package.json"],
        capture_output=True, text=True, timeout=10,
    )
    if version_result.returncode == 0:
        try:
            version = json.loads(version_result.stdout)["version"]
        except (json.JSONDecodeError, KeyError):
            pass

    if not is_valid_version(version):
        inspect = subprocess.run(
            [docker, "inspect", server_container, "--format", "{{.Config.Image}}"],
            capture_output=True, text=True, timeout=10,
        )
        if inspect.returncode == 0:
            tag = inspect.stdout.strip().split(":")[-1]
            if is_valid_version(tag):
                version = tag

    # Get env vars
    env_result = subprocess.run(
        [docker, "exec", server_container, "env"],
        capture_output=True, text=True, timeout=10,
    )
    env = {}
    for line in env_result.stdout.strip().split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

    # Get volume mounts
    try:
        mounts_result = subprocess.run(
            [docker, "inspect", server_container, "--format", "{{json .Mounts}}"],
            capture_output=True, text=True, timeout=10,
        )
        mounts = json.loads(mounts_result.stdout.strip()) if mounts_result.returncode == 0 else []
    except (json.JSONDecodeError, subprocess.SubprocessError):
        mounts = []

    upload_mount = None
    for m in mounts:
        dest = m.get("Destination", "")
        if "/upload" in dest:
            upload_mount = m.get("Source", "")
            break

    # Find exposed DB/Redis ports
    db_port = _find_exposed_port(docker, ["immich_postgres", "database"], "5432")
    redis_port = _find_exposed_port(docker, ["immich_redis", "redis"], "6379")

    return {
        "container": server_container,
        "version": version,
        "db_password": env.get("DB_PASSWORD", ""),
        "db_username": env.get("DB_USERNAME", "postgres"),
        "db_name": env.get("DB_DATABASE_NAME", "immich"),
        "db_port": db_port,
        "redis_port": redis_port,
        "upload_mount": upload_mount,
        "ml_url": env.get("IMMICH_MACHINE_LEARNING_URL", ""),
        "workers_include": env.get("IMMICH_WORKERS_INCLUDE", ""),
        "media_location": env.get("IMMICH_MEDIA_LOCATION", ""),
    }


def _find_exposed_port(docker: str, container_names: list[str], default: str) -> str:
    for name in container_names:
        result = subprocess.run(
            [docker, "port", name, default],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split(":")[-1]
    return default


# --- Server management ---

def _rebuild_sharp(server_dir: Path) -> None:
    """Rebuild Sharp native bindings against system libvips (Homebrew).

    The container has linux-arm64 Sharp. System libvips matches Docker's
    error handling for corrupt HEIF files.
    """
    log.info("Rebuilding Sharp for macOS (requires: brew install vips)...")
    npm = find_npm()
    sharp_dirs = list(server_dir.glob("node_modules/.pnpm/sharp@*/node_modules/sharp"))
    if sharp_dirs:
        result = subprocess.run(
            [npm, "rebuild"], cwd=str(sharp_dirs[0]),
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "PATH": f"/opt/homebrew/bin:{os.environ.get('PATH', '')}"},
        )
        if result.returncode != 0:
            log.error("Sharp rebuild failed: %s", result.stderr[-500:])
            log.error("Make sure libvips is installed: brew install vips")
        else:
            log.info("  Sharp rebuilt against system libvips")
    else:
        log.warning("Sharp not found in node_modules — thumbnail generation may fail")


def extract_immich_server(docker: str, container: str, version: str) -> Path:
    """Extract Immich server and build data from the running Docker container.

    Copies the pre-built server (dist/, node_modules/) and build assets
    (geodata, plugins) directly from the container. Then installs the
    macOS-native Sharp binary so image processing works outside Docker.

    This approach always matches the exact container version — no source
    downloads, no npm install, no TypeScript build.
    """
    bare_version = version.lstrip("v")
    server_dir = DATA_DIR / "server" / bare_version
    build_data = DATA_DIR / "build-data"

    if server_dir.exists() and (server_dir / "dist" / "main.js").exists():
        log.info("Using cached Immich server %s", bare_version)
        return server_dir

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Extract server from container
    (DATA_DIR / "server").mkdir(parents=True, exist_ok=True)
    staging = DATA_DIR / "server" / f"{bare_version}.staging"
    if staging.exists():
        shutil.rmtree(staging)

    log.info("Extracting server from Docker container...")
    result = subprocess.run(
        [docker, "cp", f"{container}:/usr/src/app/server", str(staging)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to extract server: {result.stderr.strip()}")

    if not (staging / "dist" / "main.js").exists():
        shutil.rmtree(staging)
        raise RuntimeError("Extracted server is missing dist/main.js")

    # Extract build data (geodata, plugins, web assets)
    if build_data.exists():
        shutil.rmtree(build_data)
    log.info("Extracting build data...")
    result = subprocess.run(
        [docker, "cp", f"{container}:/build", str(build_data)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        log.warning("Could not extract build data: %s", result.stderr.strip())
        build_data.mkdir(parents=True, exist_ok=True)

    _rebuild_sharp(staging)

    # Move to final location
    if server_dir.exists():
        shutil.rmtree(server_dir)
    staging.rename(server_dir)

    log.info("Immich server %s ready", bare_version)
    return server_dir


# --- Process management ---

def save_config(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp file + rename prevents corruption if interrupted
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(tmp, 0o600)
    tmp.rename(CONFIG_FILE)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise RuntimeError("Not set up yet. Run: python -m accelerator setup")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _get_process_start_time(pid: int) -> str | None:
    """Get process start time via ps. Used to detect PID reuse."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def write_pid(name: str, pid: int) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    start_time = _get_process_start_time(pid) or ""
    (PID_DIR / f"{name}.pid").write_text(f"{pid}\n{start_time}")


def read_pid(name: str) -> int | None:
    pid_file = PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        return None
    try:
        lines = pid_file.read_text().strip().split("\n")
        pid = int(lines[0])
        os.kill(pid, 0)  # check if process exists
        # Verify start time matches to detect PID reuse
        if len(lines) > 1 and lines[1]:
            current_start = _get_process_start_time(pid)
            if current_start and current_start != lines[1]:
                log.debug("PID %d reused (start time mismatch), cleaning up", pid)
                pid_file.unlink(missing_ok=True)
                return None
        return pid
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return None


def kill_pid(name: str) -> bool:
    pid = read_pid(name)
    if pid is None:
        return False
    try:
        # Kill the entire process group (catches Node.js child processes)
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # Wait for exit
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        # Still alive after 5s — escalate to SIGKILL
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    (PID_DIR / f"{name}.pid").unlink(missing_ok=True)
    return True


def start_service(name: str, cmd: list[str], env: dict, cwd: str) -> int:
    """Start a background service and track its PID. Returns PID."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{name}.log"
    fh = open(log_file, "a")
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        fh.close()
        raise

    # Close fh immediately — Popen duplicated the fd
    fh.close()

    write_pid(name, proc.pid)

    # Check it's still alive after a moment
    time.sleep(2)
    if proc.poll() is not None:
        log.error("%s exited immediately. Check %s", name, log_file)
        lines = log_file.read_text().strip().split("\n")
        for line in lines[-10:]:
            log.error("  %s", line)
        (PID_DIR / f"{name}.pid").unlink(missing_ok=True)
        raise RuntimeError(f"{name} failed to start")

    return proc.pid


# --- Commands ---

_JF_FFMPEG_URL = "https://repo.jellyfin.org/files/ffmpeg/macos/latest-7.x/arm64/jellyfin-ffmpeg_7.1.3-4_portable_macarm64-gpl.tar.xz"


def _ensure_jellyfin_ffmpeg() -> str:
    """Download jellyfin-ffmpeg if not present. Returns path to ffmpeg binary.

    Uses jellyfin-ffmpeg instead of Homebrew ffmpeg because it includes:
    - tonemapx filter (Immich's HDR→SDR, not in upstream ffmpeg)
    - VideoToolbox encoders
    - libwebp encoder
    All matching what Immich's Docker image uses.
    """
    jf_dir = DATA_DIR / "jellyfin-ffmpeg"
    jf_ffmpeg = jf_dir / "ffmpeg"

    if jf_ffmpeg.exists():
        # Verify it runs
        try:
            r = subprocess.run([str(jf_ffmpeg), "-version"],
                              capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return str(jf_ffmpeg)
        except (subprocess.SubprocessError, OSError):
            pass
        log.warning("Cached jellyfin-ffmpeg is broken, re-downloading...")

    log.info("Downloading jellyfin-ffmpeg (same ffmpeg Immich uses in Docker)...")
    jf_dir.mkdir(parents=True, exist_ok=True)

    import urllib.request
    tar_path = jf_dir / "jellyfin-ffmpeg.tar.xz"
    try:
        urllib.request.urlretrieve(_JF_FFMPEG_URL, str(tar_path))
    except Exception as e:
        raise RuntimeError(f"Failed to download jellyfin-ffmpeg: {e}")

    # Extract
    result = subprocess.run(
        ["tar", "xf", str(tar_path), "-C", str(jf_dir)],
        capture_output=True, text=True, timeout=60,
    )
    tar_path.unlink(missing_ok=True)

    if result.returncode != 0 or not jf_ffmpeg.exists():
        raise RuntimeError(f"Failed to extract jellyfin-ffmpeg: {result.stderr}")

    os.chmod(jf_ffmpeg, 0o755)
    ffprobe = jf_dir / "ffprobe"
    if ffprobe.exists():
        os.chmod(ffprobe, 0o755)

    log.info("  jellyfin-ffmpeg installed: %s", jf_ffmpeg)
    return str(jf_ffmpeg)


def _check_local_tools() -> tuple[str, str | None, Path | None]:
    """Check for Node.js, ffmpeg, and ML service. Returns (node, ffmpeg_path, ml_dir)."""
    node = find_node()
    log.info("Node.js: %s",
             subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip())

    # Use jellyfin-ffmpeg (same as Immich's Docker image) — has tonemapx, VideoToolbox, libwebp
    try:
        ffmpeg_path = _ensure_jellyfin_ffmpeg()
        log.info("FFmpeg: %s (jellyfin-ffmpeg, tonemapx + VideoToolbox)", ffmpeg_path)
    except RuntimeError as e:
        log.warning("Could not install jellyfin-ffmpeg: %s", e)
        # Fall back to Homebrew ffmpeg
        ffmpeg_path = None
        for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
            if os.path.isfile(p):
                ffmpeg_path = p
                log.warning("  Falling back to %s (may lack tonemapx for HDR)", p)
                break
        if not ffmpeg_path:
            log.warning("No FFmpeg found. Install: brew install ffmpeg")

    ml_dir = _find_ml_dir()
    if ml_dir:
        log.info("ML service: %s", ml_dir)
    else:
        log.warning("ML service not found — CLIP/face/OCR will use Docker ML if available")

    return node, ffmpeg_path, ml_dir


def _validate_connectivity(config: dict) -> bool:
    """Check that DB and Redis are reachable. Returns True if all OK."""
    ok = True
    if not check_port(config["db_hostname"], int(config["db_port"]), "Postgres"):
        ok = False
    if not check_port(config["redis_hostname"], int(config["redis_port"]), "Redis"):
        ok = False
    return ok


def _finalize_config(config: dict) -> None:
    """Preserve existing api_key, save config, print next steps."""
    try:
        existing = load_config()
        if existing.get("api_key") and "api_key" not in config:
            config["api_key"] = existing["api_key"]
    except RuntimeError:
        pass

    if "api_key" not in config:
        log.info("")
        log.info("Optional: add your Immich API key to enable the dashboard Re-queue button:")
        log.info("  Edit %s and add: \"api_key\": \"your-key-here\"", CONFIG_FILE)
        log.info("  Generate a key in Immich → Administration → API Keys")

    save_config(config)
    log.info("")
    log.info("Setup complete. Run: python -m accelerator start")


def _query_immich_api(base_url: str, api_key: str) -> dict:
    """Query Immich API for server info. Returns version and config."""
    import urllib.request, urllib.error
    headers = {"x-api-key": api_key} if api_key else {}

    # Get version
    req = urllib.request.Request(f"{base_url}/api/server/version", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            version = f"{data['major']}.{data['minor']}.{data['patch']}"
    except (urllib.error.URLError, KeyError) as e:
        raise RuntimeError(f"Could not reach Immich at {base_url}: {e}")

    return {"version": version, "url": base_url}


def _import_server(source: str, version: str) -> Path:
    """Import server files from a directory or tarball.

    Handles:
    - Directory containing dist/main.js (already extracted)
    - .tar.gz file (from docker cp ... | gzip)
    """
    import tarfile
    source_path = Path(source)
    bare_version = version.lstrip("v")
    server_dir = DATA_DIR / "server" / bare_version

    if source_path.is_dir():
        # Direct directory — check it has what we need
        if not (source_path / "dist" / "main.js").exists():
            raise RuntimeError(f"Not a valid server directory: {source_path} (missing dist/main.js)")
        if server_dir.exists():
            shutil.rmtree(server_dir)
        shutil.copytree(str(source_path), str(server_dir))
    elif source_path.suffix in (".gz", ".tgz") or source_path.name.endswith(".tar.gz"):
        # Tarball — extract
        if not source_path.exists():
            raise RuntimeError(f"File not found: {source_path}")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        staging = DATA_DIR / "server" / f"{bare_version}.staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        with tarfile.open(str(source_path), "r:gz") as tf:
            # Prevent path traversal from crafted tarballs
            try:
                tf.extractall(str(staging), filter='data')
            except TypeError:
                # Python < 3.11.4 doesn't support filter=
                for member in tf.getmembers():
                    resolved = (staging / member.name).resolve()
                    if not str(resolved).startswith(str(staging.resolve())):
                        raise RuntimeError(f"Unsafe path in tarball: {member.name}")
                tf.extractall(str(staging))
        # The tarball may have a top-level 'server' directory or not
        candidates = [staging, staging / "server"]
        found = None
        for c in candidates:
            if (c / "dist" / "main.js").exists():
                found = c
                break
        if not found:
            shutil.rmtree(staging)
            raise RuntimeError("Tarball does not contain dist/main.js")
        if server_dir.exists():
            shutil.rmtree(server_dir)
        found.rename(server_dir)
        # Clean up staging if it still exists
        if staging.exists():
            shutil.rmtree(staging)
    else:
        raise RuntimeError(f"Unsupported format: {source_path}. Use a directory or .tar.gz")

    _rebuild_sharp(server_dir)

    # Also import build data if a build tarball exists alongside the server
    build_data = DATA_DIR / "build-data"
    if source_path.is_file():
        for build_name in ["immich-build.tar.gz", "build.tar.gz"]:
            build_tar = source_path.parent / build_name
            if build_tar.exists():
                log.info("Importing build data from %s...", build_name)
                if build_data.exists():
                    shutil.rmtree(build_data)
                build_data.mkdir(parents=True, exist_ok=True)
                with tarfile.open(str(build_tar), "r:gz") as bf:
                    try:
                        bf.extractall(str(build_data), filter='data')
                    except TypeError:
                        bf.extractall(str(build_data))
                break
        else:
            if not build_data.exists():
                log.warning("Build data not found. Geodata/plugins may be missing.")
                log.warning("  Extract: docker cp immich_server:/build - | gzip > immich-build.tar.gz")

    log.info("Immich server %s ready", bare_version)
    return server_dir


def _setup_local(args):
    """Setup from local Docker (original behavior)."""
    log.info("Detecting Immich instance...")
    docker = find_docker()
    immich = detect_immich(docker)

    if not is_valid_version(immich["version"]):
        raise RuntimeError(
            f"Could not detect Immich version (got '{immich['version']}'). "
            "Is Immich running with a tagged release image?"
        )

    log.info("Found: %s (version %s)", immich["container"], immich["version"])
    log.info("  DB: localhost:%s (user: %s, db: %s)",
             immich["db_port"], immich["db_username"], immich["db_name"])
    log.info("  Redis: localhost:%s", immich["redis_port"])
    log.info("  Upload: %s", immich["upload_mount"] or "not detected")

    # Verify connectivity
    ok = True
    if not check_port("localhost", int(immich["db_port"]), "Postgres"):
        log.error("  Add to docker-compose database service: ports: ['127.0.0.1:5432:5432']")
        ok = False
    if not check_port("localhost", int(immich["redis_port"]), "Redis"):
        log.error("  Add to docker-compose redis service: ports: ['127.0.0.1:6379:6379']")
        ok = False
    if not ok:
        log.error("Fix the above, run 'docker compose up -d', then re-run setup.")
        return

    # Check Docker config
    upload = immich["upload_mount"]
    if immich["workers_include"] != "api" or not immich["media_location"]:
        log.warning("")
        log.warning("Docker config needed — add to your Immich docker-compose.yml:")
        log.warning("  environment:")
        log.warning("    - IMMICH_WORKERS_INCLUDE=api")
        log.warning("    - IMMICH_MACHINE_LEARNING_URL=http://host.internal:3003")
        if upload:
            log.warning("    - IMMICH_MEDIA_LOCATION=%s", upload)
            log.warning("  volumes:")
            log.warning("    - %s:%s", upload, upload)
        log.warning("")
        log.warning("After updating: docker compose up -d && python -m accelerator setup")
    else:
        log.info("  Docker: API-only mode, IMMICH_MEDIA_LOCATION=%s", immich["media_location"])

    node, ffmpeg_path, ml_dir = _check_local_tools()
    server_dir = extract_immich_server(docker, immich["container"], immich["version"])

    config = {
        "version": immich["version"],
        "server_dir": str(server_dir),
        "node": node,
        "db_hostname": "localhost",
        "db_port": immich["db_port"],
        "db_username": immich["db_username"],
        "db_password": immich["db_password"],
        "db_name": immich["db_name"],
        "redis_hostname": "localhost",
        "redis_port": immich["redis_port"],
        "upload_mount": upload,
        "ffmpeg_path": ffmpeg_path,
        "ml_dir": str(ml_dir) if ml_dir else None,
        "ml_port": 3003,
    }
    _finalize_config(config)


def _setup_remote(args):
    """Setup from remote Immich instance via API."""
    url = args.url.rstrip("/")
    api_key = args.api_key or ""

    log.info("Connecting to Immich at %s...", url)
    info = _query_immich_api(url, api_key)
    version = info["version"]
    log.info("Found Immich v%s", version)

    # Interactive prompts for DB/Redis connection
    log.info("")
    log.info("Enter connection details for the Immich database and Redis.")
    log.info("These must be reachable from this Mac (expose ports or use network routing).")
    log.info("")

    def prompt(label: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        val = input(f"  {label}{suffix}: ").strip()
        return val or default

    db_hostname = prompt("Postgres host", "localhost")
    db_port = prompt("Postgres port", "5432")
    db_username = prompt("Postgres user", "postgres")
    import getpass
    db_password = getpass.getpass("  Postgres password: ").strip()
    db_name = prompt("Database name", "immich")
    redis_hostname = prompt("Redis host", db_hostname)
    redis_port = prompt("Redis port", "6379")
    upload_mount = prompt("Upload/media path (as mounted on this Mac)")

    # Check connectivity
    config = {
        "db_hostname": db_hostname, "db_port": db_port,
        "redis_hostname": redis_hostname, "redis_port": redis_port,
    }
    if not _validate_connectivity(config):
        log.error("Cannot reach DB or Redis. Check the host/port and try again.")
        return

    node, ffmpeg_path, ml_dir = _check_local_tools()

    # Server extraction
    server_dir = None
    if args.import_server:
        server_dir = _import_server(args.import_server, version)
    else:
        # Try local Docker pull
        try:
            docker = find_docker()
            image = f"ghcr.io/immich-app/immich-server:v{version}"
            log.info("Pulling %s...", image)
            subprocess.run([docker, "pull", image], check=True, timeout=300)
            # Create temp container and extract
            container = f"immich-extract-{version}"
            subprocess.run([docker, "create", "--name", container, image],
                          capture_output=True, check=True, timeout=30)
            try:
                server_dir = extract_immich_server(docker, container, version)
            finally:
                subprocess.run([docker, "rm", container], capture_output=True, timeout=10)
        except (RuntimeError, subprocess.SubprocessError, FileNotFoundError, OSError):
            log.info("")
            log.info("Docker not available on this Mac. Extract the server on your remote host:")
            log.info("")
            log.info("  # Run on the machine where Immich's Docker runs:")
            log.info("  docker cp immich_server:/usr/src/app/server - | gzip > immich-server.tar.gz")
            log.info("  docker cp immich_server:/build - | gzip > immich-build.tar.gz")
            log.info("")
            log.info("  # Copy to this Mac and re-run:")
            log.info("  python -m accelerator setup --url %s --import-server ./immich-server.tar.gz", url)
            return

    if server_dir is None:
        raise RuntimeError("Server extraction failed. Use --import-server to provide server files.")

    config = {
        "version": version,
        "server_dir": str(server_dir),
        "node": node,
        "immich_url": url,
        "db_hostname": db_hostname,
        "db_port": db_port,
        "db_username": db_username,
        "db_password": db_password,
        "db_name": db_name,
        "redis_hostname": redis_hostname,
        "redis_port": redis_port,
        "upload_mount": upload_mount,
        "ffmpeg_path": ffmpeg_path,
        "ml_dir": str(ml_dir) if ml_dir else None,
        "ml_port": 3003,
    }
    if api_key:
        config["api_key"] = api_key
    _finalize_config(config)


def _setup_manual(_args):
    """Create a config template for manual editing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists():
        log.info("Config already exists: %s", CONFIG_FILE)
        log.info("Edit it directly, or delete it and re-run --manual for a fresh template.")
        return

    template = {
        "version": "IMMICH_VERSION (e.g. 2.6.3)",
        "server_dir": str(DATA_DIR / "server" / "VERSION"),
        "node": "/opt/homebrew/bin/node",
        "immich_url": "http://YOUR_IMMICH_HOST:2283",
        "db_hostname": "YOUR_DB_HOST",
        "db_port": "5432",
        "db_username": "postgres",
        "db_password": "YOUR_DB_PASSWORD",
        "db_name": "immich",
        "redis_hostname": "YOUR_REDIS_HOST",
        "redis_port": "6379",
        "upload_mount": "/path/to/immich/upload",
        "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
        "ml_dir": str(Path(__file__).parent.parent / "ml"),
        "ml_port": 3003,
        "api_key": "YOUR_API_KEY (optional, for dashboard re-queue)",
    }

    save_config(template)

    # Check local tools so the user knows what's missing before they start
    _check_local_tools()

    log.info("Config template created: %s", CONFIG_FILE)
    log.info("")
    log.info("Edit the config with your Immich connection details, then extract the server:")
    log.info("")
    log.info("  # On the machine where Immich's Docker runs:")
    log.info("  docker cp immich_server:/usr/src/app/server - | gzip > immich-server.tar.gz")
    log.info("  docker cp immich_server:/build - | gzip > immich-build.tar.gz")
    log.info("")
    log.info("  # Copy to this Mac, then import:")
    log.info("  python -m accelerator setup --import-server ./immich-server.tar.gz")
    log.info("")
    log.info("  # Then start:")
    log.info("  python -m accelerator start")


def cmd_setup(args):
    """Set up the accelerator. Dispatches to local, remote, or manual mode."""
    if args.manual:
        _setup_manual(args)
    elif args.import_server and not args.url:
        # Standalone import: load existing config and import server files
        config = load_config()
        server_dir = _import_server(args.import_server, config["version"])
        config["server_dir"] = str(server_dir)
        save_config(config)
        log.info("Server imported. Run: python -m accelerator start")
    elif args.url:
        _setup_remote(args)
    else:
        _setup_local(args)


def _find_ml_dir() -> Path | None:
    """Find the immich-ml-metal service directory."""
    candidates = [
        Path.home() / "immich-ml-metal",
        Path(__file__).parent.parent / "ml",
    ]
    for d in candidates:
        venv_python = d / "venv" / "bin" / "python3"
        if venv_python.exists() and (d / "src" / "main.py").exists():
            return d
    return None


def _kill_stale_processes():
    """Kill any lingering immich worker or ML processes not tracked by PID files.

    Prevents zombie workers from competing for BullMQ jobs. This catches
    processes from previous runs, manual starts, or crashed accelerator
    instances that left orphans.
    """
    stale = 0
    tracked_pids = set()
    for name in ("worker", "ml"):
        pid = read_pid(name)
        if pid:
            tracked_pids.add(pid)

    # Find all immich-related processes
    try:
        result = subprocess.run(["pgrep", "-f", "immich|src.main"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            pid = int(line.strip())
            if pid not in tracked_pids and pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGTERM)
                    stale += 1
                except OSError:
                    pass
    except (subprocess.SubprocessError, ValueError):
        pass

    # Also kill old ffmpeg-proxy/server.py if still running from v0.x
    try:
        result = subprocess.run(["pgrep", "-f", "ffmpeg-proxy/server.py"], capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    os.kill(int(line.strip()), signal.SIGTERM)
                    stale += 1
                except (OSError, ValueError):
                    pass
    except subprocess.SubprocessError:
        pass

    if stale:
        log.info("Killed %d stale process(es)", stale)
        time.sleep(1)


def cmd_start(args):
    config = load_config()

    # Kill any stale processes before starting
    _kill_stale_processes()

    # Pre-flight: verify Docker config and auto-update if version changed
    immich = {}
    try:
        docker = find_docker()
        immich = detect_immich(docker)
        if immich["workers_include"] != "api":
            log.error("Docker is still running microservices. Two workers will conflict.")
            log.error("Set IMMICH_WORKERS_INCLUDE=api in docker-compose.yml first.")
            log.error("Run 'python -m accelerator setup' for full instructions.")
            return
        if config.get("upload_mount") and immich["media_location"] != config["upload_mount"]:
            log.error("IMMICH_MEDIA_LOCATION mismatch — Docker has '%s', we expect '%s'.",
                      immich["media_location"] or "(not set)", config["upload_mount"])
            log.error("This WILL corrupt file paths in the database. Fix docker-compose.yml first.")
            return

        # Auto-update: if Docker image version changed, re-extract
        running_version = immich["version"].lstrip("v")
        cached_version = config.get("version", "").lstrip("v")
        if is_valid_version(immich["version"]) and running_version != cached_version:
            log.info("Immich updated: %s -> %s. Re-extracting server...",
                     cached_version, running_version)
            server_dir = extract_immich_server(docker, immich["container"], immich["version"])
            config["version"] = immich["version"]
            config["server_dir"] = str(server_dir)
            # Refresh connection info in case it changed
            config["db_password"] = immich["db_password"]
            config["db_port"] = immich["db_port"]
            config["redis_port"] = immich["redis_port"]
            save_config(config)
    except RuntimeError as e:
        log.warning("Could not verify Docker config (%s) — proceeding anyway", e)

    worker_pid = read_pid("worker")
    if worker_pid:
        if not args.force:
            log.info("Already running (PID %d). Use --force to restart.", worker_pid)
            return
        cmd_stop(None)

    node = config["node"]
    server_dir = config["server_dir"]

    # Worker environment
    worker_env = os.environ.copy()
    worker_env.update({
        "IMMICH_WORKERS_INCLUDE": "microservices",
        "DB_HOSTNAME": config["db_hostname"],
        "DB_PORT": config["db_port"],
        "DB_USERNAME": config["db_username"],
        "DB_PASSWORD": config.get("db_password", ""),
        "DB_DATABASE_NAME": config["db_name"],
        "REDIS_HOSTNAME": config["redis_hostname"],
        "REDIS_PORT": config["redis_port"],
        "IMMICH_MACHINE_LEARNING_URL": f"http://localhost:{config['ml_port']}",
        "PATH": str(Path(node).parent) + ":" + os.environ.get("PATH", ""),
    })

    if config.get("upload_mount"):
        worker_env["IMMICH_MEDIA_LOCATION"] = config["upload_mount"]

    # Point geodata to our managed directory (avoids needing /build/ on the host)
    build_data = DATA_DIR / "build-data"
    worker_env["IMMICH_BUILD_DATA"] = str(build_data)

    # Set up VideoToolbox ffmpeg wrapper.
    # Immich doesn't support videotoolbox as an accel option, so we put a
    # wrapper script earlier in PATH that remaps software encoders to
    # VideoToolbox hardware encoders (h264 → h264_videotoolbox, etc.)
    wrapper_dir = DATA_DIR / "bin"
    wrapper_src = Path(__file__).parent / "ffmpeg-wrapper.sh"
    if config.get("ffmpeg_path") and wrapper_src.exists():
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        wrapper_dst = wrapper_dir / "ffmpeg"
        # Inject the real ffmpeg path into the wrapper (may differ from /opt/homebrew/bin)
        wrapper_content = wrapper_src.read_text().replace(
            'REAL_FFMPEG="/opt/homebrew/bin/ffmpeg"',
            f'REAL_FFMPEG="{config["ffmpeg_path"]}"',
        )
        if not wrapper_dst.exists() or wrapper_dst.read_text() != wrapper_content:
            wrapper_dst.write_text(wrapper_content)
            os.chmod(wrapper_dst, 0o755)
        # Wrapper dir first in PATH, and set FFMPEG_PATH so fluent-ffmpeg uses our wrapper
        worker_env["PATH"] = f"{wrapper_dir}:{Path(config['ffmpeg_path']).parent}:{worker_env['PATH']}"
        worker_env["FFMPEG_PATH"] = str(wrapper_dst)
    elif config.get("ffmpeg_path"):
        worker_env["PATH"] = str(Path(config["ffmpeg_path"]).parent) + ":" + worker_env["PATH"]

    # Start ML service
    ml_started_here = False
    ml_pid = read_pid("ml")
    if not ml_pid and config.get("ml_dir"):
        ml_dir = Path(config["ml_dir"])
        ml_python = ml_dir / "venv" / "bin" / "python3"
        if ml_python.exists():
            log.info("Starting ML service...")
            try:
                ml_pid = start_service("ml", [str(ml_python), "-m", "src.main"],
                                       os.environ.copy(), str(ml_dir))
                ml_started_here = True
                log.info("  ML service running (PID %d)", ml_pid)
            except RuntimeError:
                log.warning("  ML service failed to start — CLIP/face/OCR unavailable")
    elif ml_pid:
        log.info("ML service already running (PID %d)", ml_pid)

    # Start native Immich microservices worker
    log.info("Starting Immich worker (version %s)...", config["version"])
    try:
        worker_pid = start_service("worker", [node, "dist/main.js"],
                                   worker_env, server_dir)
    except RuntimeError:
        if ml_started_here:
            log.info("Stopping ML service (worker failed)...")
            kill_pid("ml")
        raise

    log.info("  Worker running (PID %d)", worker_pid)
    log.info("")
    log.info("Immich Accelerator running")
    log.info("  Worker log: %s/worker.log", LOG_DIR)
    log.info("  ML log:     %s/ml.log", LOG_DIR)


def cmd_stop(_args):
    stopped = False
    if kill_pid("worker"):
        log.info("Worker stopped")
        stopped = True
    if kill_pid("ml"):
        log.info("ML service stopped")
        stopped = True
    if not stopped:
        log.info("Nothing running")


def cmd_status(_args):
    worker_pid = read_pid("worker")
    ml_pid = read_pid("ml")

    if not worker_pid and not ml_pid:
        log.info("Not running")
        return

    log.info("Worker:     %s", f"running (PID {worker_pid})" if worker_pid else "stopped")
    log.info("ML service: %s", f"running (PID {ml_pid})" if ml_pid else "stopped")

    if CONFIG_FILE.exists():
        config = load_config()
        log.info("Version:    %s", config.get("version", "?"))
        if config.get("ffmpeg_path"):
            log.info("FFmpeg:     %s (VideoToolbox)", config["ffmpeg_path"])


def cmd_logs(args):
    target = args.service or "worker"
    log_file = LOG_DIR / f"{target}.log"
    if not log_file.exists():
        print(f"No log file: {log_file}")
        return
    os.execvp("tail", ["tail", "-f", str(log_file)])


def cmd_update(_args):
    config = load_config()
    docker = find_docker()
    immich = detect_immich(docker)

    current = config.get("version", "?")
    running = immich["version"]

    if not is_valid_version(running):
        raise RuntimeError(f"Could not detect Immich version (got '{running}')")

    if current.lstrip("v") == running.lstrip("v"):
        log.info("Already up to date: %s", current)
        return

    log.info("Update available: %s -> %s", current, running)
    log.info("Stopping services for update...")
    cmd_stop(None)

    server_dir = extract_immich_server(docker, immich["container"], running)

    updates = {
        "version": running,
        "server_dir": str(server_dir),
        "db_password": immich["db_password"],
        "db_username": immich["db_username"],
        "db_name": immich["db_name"],
        "db_port": immich["db_port"],
        "redis_port": immich["redis_port"],
    }
    # Only update upload_mount if Docker detection found one
    # (avoid wiping a valid config with None)
    if immich["upload_mount"]:
        updates["upload_mount"] = immich["upload_mount"]
    config.update(updates)
    save_config(config)

    log.info("Updated to %s. Run: python -m accelerator start", running)


def cmd_watch(_args):
    """Monitor services and restart on crash. Detects Docker updates.

    Suitable for launchd KeepAlive — runs forever, checking every 30s.
    """
    log.info("Watching services (Ctrl+C to stop)...")

    # First ensure everything is running
    if not read_pid("worker") or not read_pid("ml"):
        log.info("Services not running, starting...")
        cmd_start(argparse.Namespace(force=True))

    check_count = 0
    while True:
        try:
            time.sleep(30)
            config = load_config()  # reload each cycle (setup may have changed it)

            # Check ML
            if not read_pid("ml"):
                log.warning("ML service crashed — restarting...")
                ml_dir = Path(config.get("ml_dir", ""))
                ml_python = ml_dir / "venv" / "bin" / "python3"
                if ml_python.exists():
                    try:
                        pid = start_service("ml", [str(ml_python), "-m", "src.main"],
                                            os.environ.copy(), str(ml_dir))
                        log.info("  ML restarted (PID %d)", pid)
                    except RuntimeError:
                        log.error("  ML restart failed")

            # Check worker
            if not read_pid("worker"):
                log.warning("Worker crashed — restarting...")
                try:
                    cmd_start(argparse.Namespace(force=True))
                except RuntimeError:
                    log.error("  Worker restart failed, will retry in 30s")

            # Every 5 min, check if Docker updated Immich
            check_count += 1
            if check_count >= 10:
                check_count = 0
                try:
                    docker = find_docker()
                    immich = detect_immich(docker)
                    cached = config.get("version", "").lstrip("v")
                    running = immich["version"].lstrip("v")
                    if is_valid_version(immich["version"]) and running != cached:
                        log.info("Immich updated: %s -> %s. Restarting with new version...",
                                 cached, running)
                        cmd_stop(None)
                        # Re-extract server for new version
                        server_dir = extract_immich_server(docker, immich["container"], immich["version"])
                        config["version"] = immich["version"]
                        config["server_dir"] = str(server_dir)
                        save_config(config)
                        cmd_start(argparse.Namespace(force=True))
                except RuntimeError:
                    pass  # Docker might be mid-restart, try again next cycle

        except KeyboardInterrupt:
            log.info("Watch stopped")
            return


def cmd_dashboard(args):
    """Start the web dashboard."""
    config = load_config()
    # Import relative to this package (works whether package is named
    # 'accelerator' or 'accelerator-test' during development)
    import importlib
    dashboard_mod = importlib.import_module(".dashboard", package=__package__)
    log.info("Starting dashboard on port %d...", args.port)
    dashboard_mod.run_dashboard(config, port=args.port)


# --- Main ---

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        prog="accelerator",
        description="Immich Accelerator — native macOS microservices worker",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    setup_p = sub.add_parser("setup", help="Detect Immich, download server, configure")
    setup_p.add_argument("--url", help="Remote Immich URL (e.g. http://nas:2283)")
    setup_p.add_argument("--api-key", help="Immich API key (for remote setup)")
    setup_p.add_argument("--manual", action="store_true", help="Create config template for manual editing")
    setup_p.add_argument("--import-server", metavar="DIR", help="Import server from extracted directory or tarball")
    start_p = sub.add_parser("start", help="Start native worker + ML")
    start_p.add_argument("--force", action="store_true", help="Restart if running")
    sub.add_parser("stop", help="Stop native services")
    sub.add_parser("status", help="Show what's running")
    logs_p = sub.add_parser("logs", help="Tail service logs")
    logs_p.add_argument("service", nargs="?", choices=["worker", "ml"], default="worker")
    sub.add_parser("update", help="Update to match Immich version")
    sub.add_parser("watch", help="Monitor services, restart on crash (for launchd)")
    dash_p = sub.add_parser("dashboard", help="Web dashboard (http://localhost:8420)")
    dash_p.add_argument("--port", type=int, default=8420, help="Dashboard port")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        {"setup": cmd_setup, "start": cmd_start, "stop": cmd_stop,
         "status": cmd_status, "logs": cmd_logs, "update": cmd_update,
         "watch": cmd_watch, "dashboard": cmd_dashboard,
         }[args.command](args)
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
