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

__version__ = "1.0.0"

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

    # Install macOS-native Sharp binaries
    # The container has linux-arm64; we need darwin-arm64 + libvips for HEIF
    log.info("Installing native Sharp binaries for macOS...")
    npm = find_npm()
    sharp_dirs = list(staging.glob("node_modules/.pnpm/sharp@*/node_modules/sharp"))
    if sharp_dirs:
        sharp_dir = sharp_dirs[0]
        for pkg in ["@img/sharp-darwin-arm64", "@img/sharp-libvips-darwin-arm64"]:
            result = subprocess.run(
                [npm, "install", pkg, "--no-save"],
                cwd=str(sharp_dir),
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                log.warning("%s install failed: %s", pkg, result.stderr[-300:])
            else:
                log.info("  %s ready", pkg)
    else:
        log.warning("Sharp not found in node_modules — thumbnail generation may fail")

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

def cmd_setup(_args):
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

    # Check Docker config — IMMICH_MEDIA_LOCATION must match
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
            log.warning("    # IMPORTANT: mount path must match IMMICH_MEDIA_LOCATION exactly")
            log.warning("    - %s:%s  # (instead of %s:/usr/src/app/upload)", upload, upload, upload)
        log.warning("")
        log.warning("WHY: Both Docker and the native worker must agree on file paths.")
        log.warning("IMMICH_MEDIA_LOCATION tells Immich where files live. If Docker and")
        log.warning("the native worker disagree, Immich will rewrite all file paths in")
        log.warning("the database on every restart. Setting the same value on both prevents this.")
        log.warning("")
        log.warning("After updating: docker compose up -d && python -m accelerator setup")
    else:
        log.info("  Docker: API-only mode, IMMICH_MEDIA_LOCATION=%s", immich["media_location"])

    # Check Node.js
    node = find_node()
    log.info("Node.js: %s",
             subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip())

    # Download and build
    server_dir = extract_immich_server(docker, immich["container"], immich["version"])

    # Check for ffmpeg with VideoToolbox
    ffmpeg_path = None
    for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.isfile(p):
            result = subprocess.run([p, "-hwaccels"], capture_output=True, text=True, timeout=5)
            if "videotoolbox" in result.stdout.lower():
                ffmpeg_path = p
                log.info("FFmpeg: %s (VideoToolbox)", ffmpeg_path)
                break
    if not ffmpeg_path:
        log.warning("No FFmpeg with VideoToolbox found. Install: brew install ffmpeg")

    # Find ML service
    ml_dir = _find_ml_dir()
    if ml_dir:
        log.info("ML service: %s", ml_dir)
    else:
        log.warning("ML service not found — CLIP/face/OCR will use Docker ML if available")

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
    save_config(config)

    log.info("")
    log.info("Setup complete. Run: python -m accelerator start")


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


def cmd_start(args):
    config = load_config()

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
        "DB_PASSWORD": immich.get("db_password", config.get("db_password", "")),
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
        if not wrapper_dst.exists() or wrapper_dst.read_text() != wrapper_src.read_text():
            shutil.copy2(wrapper_src, wrapper_dst)
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
                        extract_immich_server(docker, immich["container"], immich["version"])
                        config["version"] = immich["version"]
                        config["server_dir"] = str(DATA_DIR / "server" / running)
                        save_config(config)
                        cmd_start(argparse.Namespace(force=True))
                except RuntimeError:
                    pass  # Docker might be mid-restart, try again next cycle

        except KeyboardInterrupt:
            log.info("Watch stopped")
            return


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

    sub.add_parser("setup", help="Detect Immich, download server, configure")
    start_p = sub.add_parser("start", help="Start native worker + ML")
    start_p.add_argument("--force", action="store_true", help="Restart if running")
    sub.add_parser("stop", help="Stop native services")
    sub.add_parser("status", help="Show what's running")
    logs_p = sub.add_parser("logs", help="Tail service logs")
    logs_p.add_argument("service", nargs="?", choices=["worker", "ml"], default="worker")
    sub.add_parser("update", help="Update to match Immich version")
    sub.add_parser("watch", help="Monitor services, restart on crash (for launchd)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        {"setup": cmd_setup, "start": cmd_start, "stop": cmd_stop,
         "status": cmd_status, "logs": cmd_logs, "update": cmd_update,
         "watch": cmd_watch,
         }[args.command](args)
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
