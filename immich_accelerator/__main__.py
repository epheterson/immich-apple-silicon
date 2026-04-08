"""Immich Accelerator — run Immich microservices natively on macOS.

Usage:
    python -m immich_accelerator setup     # detect Immich, checkout code, configure
    python -m immich_accelerator start     # start native worker + ML service
    python -m immich_accelerator stop      # stop native services
    python -m immich_accelerator status    # show what's running
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


SYNTHETIC_CONF = Path("/etc/synthetic.d/immich-accelerator")


def _build_link_ok() -> bool:
    """Check if /build points to our build-data directory."""
    build_data = DATA_DIR / "build-data"
    target = Path("/build")
    try:
        return target.exists() and target.resolve() == build_data.resolve()
    except OSError:
        return False


def _ensure_build_link():
    """Ensure /build exists on macOS, pointing to our build-data directory.

    Immich stores absolute paths like /build/corePlugin/dist/plugin.wasm in
    its shared Postgres DB. In split-worker setups, both Docker and native
    workers need /build to resolve. macOS SIP prevents creating directories
    at /, but /etc/synthetic.d/ provides Apple's mechanism for root-level
    synthetic symlinks. Requires sudo once during setup.
    """
    build_data = DATA_DIR / "build-data"
    build_data.mkdir(parents=True, exist_ok=True)

    if _build_link_ok():
        # Migrate legacy synthetic.conf entry to synthetic.d if needed
        if not SYNTHETIC_CONF.exists():
            legacy = Path("/etc/synthetic.conf")
            if legacy.exists() and "build\t" in legacy.read_text():
                relative_target = str(build_data).lstrip("/")
                entry = f"build\t{relative_target}\n"
                try:
                    subprocess.run(
                        ["sudo", "mkdir", "-p", "/etc/synthetic.d"],
                        capture_output=True,
                        timeout=30,
                    )
                    subprocess.run(
                        ["sudo", "tee", str(SYNTHETIC_CONF)],
                        input=entry,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    # Remove from legacy conf
                    content = legacy.read_text()
                    lines = [
                        l
                        for l in content.splitlines(keepends=True)
                        if not l.startswith("build\t")
                    ]
                    new_content = "".join(lines)
                    if new_content.strip():
                        subprocess.run(
                            ["sudo", "tee", str(legacy)],
                            input=new_content,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    log.info("Migrated /build link to /etc/synthetic.d/")
                except (OSError, subprocess.SubprocessError):
                    pass  # Non-fatal, link still works
        return True

    if Path("/build").exists():
        log.warning("/build exists but doesn't point to our build-data.")
        log.warning("  Plugin paths may not resolve correctly.")
        return False

    # Check if already configured but not yet active (needs reboot)
    if SYNTHETIC_CONF.exists():
        log.info("/build link configured but not yet active.")
        log.info("  Reboot to activate it.")
        return False

    log.info("")
    log.info("Immich stores plugin paths as /build/... in its database.")
    log.info("To make these paths work on macOS, we need to create:")
    log.info("  /build → ~/.immich-accelerator/build-data")
    log.info("This uses macOS synthetic links (requires sudo once).")
    log.info("")

    try:
        answer = input("Create /build link? [Y/n] ").strip().lower()
    except EOFError:
        return False
    if answer and answer != "y":
        return False

    # Write our own file in /etc/synthetic.d/ (avoids touching shared synthetic.conf)
    relative_target = str(build_data).lstrip("/")
    entry = f"build\t{relative_target}\n"
    try:
        result = subprocess.run(
            ["sudo", "mkdir", "-p", "/etc/synthetic.d"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("Failed to create /etc/synthetic.d/")
            return False
        result = subprocess.run(
            ["sudo", "tee", str(SYNTHETIC_CONF)],
            input=entry,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("Failed to write %s: %s", SYNTHETIC_CONF, result.stderr.strip())
            return False
    except subprocess.SubprocessError as e:
        log.warning("Failed to configure /build link: %s", e)
        return False

    # Try to activate without reboot
    apfs_util = "/System/Library/Filesystems/apfs.fs/Contents/Resources/apfs.util"
    if Path(apfs_util).exists():
        result = subprocess.run(
            ["sudo", apfs_util, "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and _build_link_ok():
            log.info("/build link created successfully")
            return True

    log.info("/build link configured. Reboot to activate it.")
    return False


def _remove_build_link():
    """Remove /build synthetic link during uninstall."""
    removed = False

    # Remove synthetic.d file (v1.3.3+)
    if SYNTHETIC_CONF.exists():
        log.info("Removing /build link (requires sudo)...")
        try:
            result = subprocess.run(
                ["sudo", "rm", str(SYNTHETIC_CONF)],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                removed = True
            else:
                log.warning("  Could not remove %s", SYNTHETIC_CONF)
        except subprocess.SubprocessError as e:
            log.warning("  Could not remove %s: %s", SYNTHETIC_CONF, e)

    # Also clean legacy entry from /etc/synthetic.conf (pre-v1.3.3)
    legacy_conf = Path("/etc/synthetic.conf")
    if legacy_conf.exists():
        try:
            content = legacy_conf.read_text()
            if "build\t" in content:
                lines = [
                    l
                    for l in content.splitlines(keepends=True)
                    if not l.startswith("build\t")
                ]
                new_content = "".join(lines)
                if not removed:
                    log.info(
                        "Removing /build link from synthetic.conf (requires sudo)..."
                    )
                if new_content.strip():
                    subprocess.run(
                        ["sudo", "tee", str(legacy_conf)],
                        input=new_content,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                else:
                    subprocess.run(
                        ["sudo", "rm", str(legacy_conf)],
                        capture_output=True,
                        timeout=10,
                    )
                removed = True
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("  Could not clean synthetic.conf: %s", e)

    if removed:
        log.info("  /build link removed. Reboot to fully deactivate.")


def find_binary(name: str, paths: list[str], install_hint: str) -> str:
    for p in paths:
        if os.path.isfile(p):
            return p
    raise RuntimeError(f"{name} not found. {install_hint}")


def _ensure_homebrew() -> str | None:
    """Find Homebrew, or offer to install it. Returns brew path or None."""
    for p in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
        if os.path.isfile(p):
            return p
    try:
        answer = input("  Homebrew not found. Install it? [Y/n] ").strip().lower()
    except EOFError:
        return None
    if answer and answer != "y":
        return None
    log.info("  Installing Homebrew...")
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            "curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh | /bin/bash",
        ],
        capture_output=False,
        timeout=600,
    )
    if result.returncode == 0:
        for p in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
            if os.path.isfile(p):
                return p
    log.warning("  Homebrew installation failed. Install manually: https://brew.sh")
    return None


def _brew_install(package: str) -> bool:
    """Prompt to install a Homebrew package. Returns True if installed."""
    brew = _ensure_homebrew()
    if not brew:
        return False

    try:
        answer = (
            input(f"  {package} not found. Install with Homebrew? [Y/n] ")
            .strip()
            .lower()
        )
    except EOFError:
        return False
    if answer and answer != "y":
        return False

    log.info("  Installing %s...", package)
    result = subprocess.run(
        [brew, "install", package], capture_output=False, timeout=300
    )
    return result.returncode == 0


def find_docker() -> str:
    return find_binary(
        "Docker",
        [
            "/usr/local/bin/docker",
            "/opt/homebrew/bin/docker",
            "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
        ],
        "Install Docker Desktop or OrbStack.",
    )


def find_node() -> str:
    paths = ["/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for p in paths:
        if os.path.isfile(p):
            return p
    if _brew_install("node"):
        for p in paths:
            if os.path.isfile(p):
                return p
    raise RuntimeError("Node.js not found. Install with: brew install node")


def find_npm() -> str:
    return find_binary(
        "npm",
        ["/opt/homebrew/bin/npm", "/usr/local/bin/npm"],
        "Install with: brew install node",
    )


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
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Docker not running or not accessible: {result.stderr.strip()}"
        )

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
        raise RuntimeError(
            "No Immich server container found. Is Immich running in Docker?"
        )

    # Get version from package.json inside the container
    version = "unknown"
    version_result = subprocess.run(
        [docker, "exec", server_container, "cat", "/usr/src/app/server/package.json"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if version_result.returncode == 0:
        try:
            version = json.loads(version_result.stdout)["version"]
        except (json.JSONDecodeError, KeyError):
            pass

    if not is_valid_version(version):
        inspect = subprocess.run(
            [docker, "inspect", server_container, "--format", "{{.Config.Image}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if inspect.returncode == 0:
            tag = inspect.stdout.strip().split(":")[-1]
            if is_valid_version(tag):
                version = tag

    # Get env vars
    env_result = subprocess.run(
        [docker, "exec", server_container, "env"],
        capture_output=True,
        text=True,
        timeout=10,
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
            capture_output=True,
            text=True,
            timeout=10,
        )
        mounts = (
            json.loads(mounts_result.stdout.strip())
            if mounts_result.returncode == 0
            else []
        )
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
            capture_output=True,
            text=True,
            timeout=5,
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
            [npm, "rebuild"],
            cwd=str(sharp_dirs[0]),
            capture_output=True,
            text=True,
            timeout=180,
            env={
                **os.environ,
                "PATH": f"/opt/homebrew/bin:{os.environ.get('PATH', '')}",
            },
        )
        if result.returncode != 0:
            log.error("Sharp rebuild failed: %s", result.stderr[-500:])
            log.error("Make sure libvips is installed: brew install vips")
        else:
            log.info("  Sharp rebuilt against system libvips")
    else:
        log.warning("Sharp not found in node_modules — thumbnail generation may fail")


def download_immich_server(version: str) -> Path:
    """Download Immich server directly from ghcr.io — no Docker needed.

    Fetches the container image layers from GitHub Container Registry,
    extracts the server and build data. Works without Docker installed.
    """
    import urllib.request as urlreq
    import tarfile

    bare_version = version.lstrip("v")
    server_dir = DATA_DIR / "server" / bare_version

    if server_dir.exists() and (server_dir / "dist" / "main.js").exists():
        log.info("Using cached Immich server %s", bare_version)
        return server_dir

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    registry = "https://ghcr.io"
    image = "immich-app/immich-server"
    tag = f"v{bare_version}"

    log.info("Downloading Immich server %s from ghcr.io...", tag)

    # Get anonymous auth token
    token_resp = urlreq.urlopen(
        f"{registry}/token?service=ghcr.io&scope=repository:{image}:pull", timeout=10
    )
    token = json.loads(token_resp.read())["token"]
    headers = {"Authorization": f"Bearer {token}"}

    def _get(url, accept=None):
        hdrs = {**headers}
        if accept:
            hdrs["Accept"] = accept
        req = urlreq.Request(url, headers=hdrs)
        return urlreq.urlopen(req, timeout=300)

    # Get image index → find amd64 manifest (server is JS, arch doesn't matter)
    index = json.loads(
        _get(
            f"{registry}/v2/{image}/manifests/{tag}",
            accept="application/vnd.oci.image.index.v1+json",
        ).read()
    )

    platform_digest = None
    for m in index.get("manifests", []):
        p = m.get("platform", {})
        if p.get("architecture") == "amd64" and p.get("os") == "linux":
            platform_digest = m["digest"]
            break
    if not platform_digest:
        raise RuntimeError("Could not find amd64 manifest for Immich server")

    # Get image manifest → layer list
    manifest = json.loads(
        _get(
            f"{registry}/v2/{image}/manifests/{platform_digest}",
            accept="application/vnd.oci.image.manifest.v1+json",
        ).read()
    )

    layers = manifest.get("layers", [])
    staging = DATA_DIR / "server" / f"{bare_version}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    build_data = DATA_DIR / "build-data"
    if build_data.exists():
        shutil.rmtree(build_data)
    build_data.mkdir(parents=True, exist_ok=True)

    # Download and extract layers containing server and build data.
    # Process all layers largest-first. Docker COPY instructions each create
    # a separate layer — corePlugin (WASM) may be in a small layer, so we
    # can't skip by size. Build data accumulates across multiple layers.
    found_server = False
    found_build = False
    sorted_layers = list(enumerate(layers))
    sorted_layers.sort(key=lambda x: x[1]["size"], reverse=True)

    import io

    for i, layer in sorted_layers:
        size_mb = layer["size"] / 1024 / 1024
        # Break when we have server + build data. For Immich 2.7+ we also
        # need corePlugin, which may be in a separate small layer.
        has_core = (build_data / "corePlugin" / "manifest.json").exists()
        if found_server and found_build and has_core:
            break
        # For pre-2.7 images (no corePlugin), skip remaining small layers
        if found_server and found_build and size_mb < 1:
            break
        digest = layer["digest"]
        if size_mb >= 1:
            log.info(
                "  Downloading layer %d/%d (%.0fMB)...",
                i + 1,
                len(layers),
                size_mb,
            )
        else:
            log.debug(
                "  Downloading layer %d/%d (%.0fKB)...",
                i + 1,
                len(layers),
                layer["size"] / 1024,
            )

        try:
            resp = _get(f"{registry}/v2/{image}/blobs/{digest}")
            data = resp.read()

            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                names = tf.getnames()
                has_server = any(n.startswith("usr/src/app/server/") for n in names)
                has_build = any(n.startswith("build/") for n in names)

                if has_server and not found_server:
                    log.info("    Extracting server...")
                    # Extract all server members at once — pnpm symlinks need
                    # their targets to exist, so per-member extract breaks.
                    import tempfile

                    with tempfile.TemporaryDirectory() as tmpdir:
                        try:
                            tf.extractall(tmpdir, filter="tar")
                        except TypeError:
                            tf.extractall(tmpdir)
                        src = Path(tmpdir) / "usr" / "src" / "app" / "server"
                        if src.exists():
                            if staging.exists():
                                shutil.rmtree(staging)
                            shutil.copytree(str(src), str(staging), symlinks=True)
                    found_server = True

                if has_build:
                    log.info("    Extracting build data...")
                    for member in tf.getmembers():
                        if member.name.startswith("build/"):
                            # Rewrite "build/" -> "build-data/" so files land
                            # directly in our IMMICH_BUILD_DATA directory
                            member.name = "build-data" + member.name[5:]
                            try:
                                tf.extract(
                                    member, str(build_data.parent), filter="data"
                                )
                            except TypeError:
                                tf.extract(member, str(build_data.parent))
                    found_build = True

        except Exception as e:
            log.warning("  Layer %d failed: %s", i, e)
            continue

    if not found_server:
        shutil.rmtree(staging)
        raise RuntimeError("Could not find server in image layers")

    if not (staging / "dist" / "main.js").exists():
        shutil.rmtree(staging)
        raise RuntimeError("Downloaded server is missing dist/main.js")

    _rebuild_sharp(staging)

    # Move to final location
    if server_dir.exists():
        shutil.rmtree(server_dir)
    staging.rename(server_dir)

    log.info("Immich server %s ready (downloaded from ghcr.io)", bare_version)
    return server_dir


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
        capture_output=True,
        text=True,
        timeout=120,
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
        capture_output=True,
        text=True,
        timeout=60,
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
        raise RuntimeError("Not set up yet. Run: python -m immich_accelerator setup")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _get_process_start_time(pid: int) -> str | None:
    """Get process start time via ps. Used to detect PID reuse."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
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
            cmd,
            cwd=cwd,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
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
            r = subprocess.run(
                [str(jf_ffmpeg), "-version"], capture_output=True, text=True, timeout=5
            )
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
        capture_output=True,
        text=True,
        timeout=60,
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


def _ensure_vips() -> None:
    """Check for libvips (needed for Sharp). Offer to install if missing."""
    vips_paths = ["/opt/homebrew/lib/libvips.dylib", "/usr/local/lib/libvips.dylib"]
    for p in vips_paths:
        if os.path.isfile(p):
            return
    # Also check via pkg-config
    r = subprocess.run(
        ["pkg-config", "--exists", "vips"], capture_output=True, timeout=5
    )
    if r.returncode == 0:
        return
    if not _brew_install("vips"):
        log.warning(
            "libvips not found. Sharp rebuild may fail. Install: brew install vips"
        )


def _check_local_tools() -> tuple[str, str | None, Path | None]:
    """Check for Node.js, ffmpeg, libvips, and ML service. Returns (node, ffmpeg_path, ml_dir)."""
    node = find_node()
    log.info(
        "Node.js: %s",
        subprocess.run(
            [node, "--version"], capture_output=True, text=True
        ).stdout.strip(),
    )

    _ensure_vips()

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
        log.warning(
            "ML service not found — CLIP/face/OCR will use Docker ML if available"
        )

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
        log.info(
            "Optional: add your Immich API key to enable the dashboard Re-queue button:"
        )
        log.info('  Edit %s and add: "api_key": "your-key-here"', CONFIG_FILE)
        log.info("  Generate a key in Immich → Administration → API Keys")

    save_config(config)

    # Ensure /build firmlink for plugin path compatibility (Immich 2.7+)
    _ensure_build_link()

    # Auto-start services
    log.info("")
    try:
        answer = input("  Start Immich Accelerator now? [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if not answer or answer == "y":
        cmd_start(argparse.Namespace(force=True))

    # Offer to install launchd service (watch mode — manages worker, ML, and dashboard)
    plist_src = (
        Path(__file__).parent.parent / "launchd" / "com.immich.accelerator.plist"
    )
    plist_dst = (
        Path.home() / "Library" / "LaunchAgents" / "com.immich.accelerator.plist"
    )

    if plist_src.exists() and not plist_dst.exists():
        try:
            answer = (
                input("  Install as system service (auto-starts on login)? [Y/n] ")
                .strip()
                .lower()
            )
        except EOFError:
            answer = "n"
        if not answer or answer == "y":
            content = plist_src.read_text()
            repo_dir = str(Path(__file__).parent.parent.resolve())
            content = content.replace("/path/to/immich-apple-silicon", repo_dir)
            content = content.replace("/opt/homebrew/bin/python3", sys.executable)
            plist_dst.parent.mkdir(parents=True, exist_ok=True)
            plist_dst.write_text(content)
            subprocess.run(
                ["launchctl", "load", str(plist_dst)], capture_output=True, timeout=10
            )
            log.info("  Installed (auto-starts worker, ML, and dashboard on login)")

    log.info("")
    log.info("Immich Accelerator is running.")


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
            raise RuntimeError(
                f"Not a valid server directory: {source_path} (missing dist/main.js)"
            )
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
                tf.extractall(str(staging), filter="data")
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
        raise RuntimeError(
            f"Unsupported format: {source_path}. Use a directory or .tar.gz"
        )

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
                        bf.extractall(str(build_data), filter="data")
                    except TypeError:
                        bf.extractall(str(build_data))
                break
        else:
            if not build_data.exists():
                log.warning("Build data not found. Geodata/plugins may be missing.")
                log.warning(
                    "  Extract: docker cp immich_server:/build - | gzip > immich-build.tar.gz"
                )

    log.info("Immich server %s ready", bare_version)
    return server_dir


def _find_compose_file(docker: str) -> Path | None:
    """Find the docker-compose.yml for the Immich stack."""
    # Ask Docker for the compose file path
    try:
        r = subprocess.run(
            [
                docker,
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project.working_dir"}}',
                "immich_server",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            compose_dir = Path(r.stdout.strip())
            for name in [
                "docker-compose.yml",
                "docker-compose.yaml",
                "compose.yml",
                "compose.yaml",
            ]:
                f = compose_dir / name
                if f.exists():
                    return f
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _configure_docker(docker: str, immich: dict, upload: str | None) -> None:
    """Show required docker-compose changes, offer to open editor, retry until connected."""
    compose_file = _find_compose_file(docker)
    ml_url = "http://host.internal:3003"  # OrbStack; Docker Desktop uses host.docker.internal

    log.info("")
    log.info("Add these to your docker-compose.yml (immich-server service):")
    log.info("")
    log.info("  environment:")
    log.info("    - IMMICH_WORKERS_INCLUDE=api")
    log.info("    - IMMICH_MACHINE_LEARNING_URL=%s", ml_url)
    if upload:
        log.info("    - IMMICH_MEDIA_LOCATION=%s", upload)
        log.info("  volumes:")
        log.info("    - %s:%s", upload, upload)
    log.info("")
    log.info("  And expose ports on database and redis services:")
    log.info("    ports: ['127.0.0.1:5432:5432']   # database")
    log.info("    ports: ['127.0.0.1:6379:6379']   # redis")
    log.info("")
    log.info("  Docker Desktop users: use http://host.docker.internal:3003 instead")

    # Offer to open in editor
    if compose_file:
        log.info("")
        log.info("  Found: %s", compose_file)
        try:
            answer = input("  Open in your editor? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if not answer or answer == "y":
            editor = os.environ.get("EDITOR", "nano")
            subprocess.run([editor, str(compose_file)])

    # Retry loop — wait for user to apply changes and restart Docker
    log.info("")
    log.info("After editing, run 'docker compose up -d' in another terminal.")
    while True:
        try:
            answer = (
                input("  Press Enter to check connection (q to finish later)... ")
                .strip()
                .lower()
            )
        except EOFError:
            break
        if answer == "q":
            log.info("  Run 'python -m immich_accelerator start' when Docker is ready.")
            break

        # Check connectivity
        db_ok = check_port("localhost", int(immich.get("db_port", "5432")), "Postgres")
        redis_ok = check_port(
            "localhost", int(immich.get("redis_port", "6379")), "Redis"
        )

        if db_ok and redis_ok:
            # Re-detect to check config
            try:
                fresh = detect_immich(docker)
                if fresh["workers_include"] == "api":
                    log.info("  ✓ Connected! Docker configured correctly.")
                    return
                else:
                    log.info(
                        "  ✗ Ports OK but IMMICH_WORKERS_INCLUDE not set to 'api'."
                    )
                    log.info("    Add it to docker-compose.yml and restart.")
            except RuntimeError:
                log.info(
                    "  ✗ Docker may still be restarting — try again in a few seconds."
                )
        else:
            if not db_ok:
                log.info("  ✗ Postgres not reachable at localhost:5432")
            if not redis_ok:
                log.info("  ✗ Redis not reachable at localhost:6379")


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
    log.info(
        "  DB: localhost:%s (user: %s, db: %s)",
        immich["db_port"],
        immich["db_username"],
        immich["db_name"],
    )
    log.info("  Redis: localhost:%s", immich["redis_port"])
    log.info("  Upload: %s", immich["upload_mount"] or "not detected")

    # Install dependencies and extract server first (doesn't need Docker config)
    upload = immich["upload_mount"]

    node, ffmpeg_path, ml_dir = _check_local_tools()
    server_dir = extract_immich_server(docker, immich["container"], immich["version"])

    # Now handle Docker config — guide user through compose changes if needed
    if immich["workers_include"] != "api" or not immich["media_location"]:
        _configure_docker(docker, immich, upload)
    else:
        log.info(
            "  Docker: API-only mode, IMMICH_MEDIA_LOCATION=%s",
            immich["media_location"],
        )

    # Re-detect after potential Docker restart
    try:
        immich = detect_immich(docker)
    except RuntimeError:
        pass

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
    log.info(
        "These must be reachable from this Mac (expose ports or use network routing)."
    )
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
        "db_hostname": db_hostname,
        "db_port": db_port,
        "redis_hostname": redis_hostname,
        "redis_port": redis_port,
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
            subprocess.run(
                [docker, "create", "--name", container, image],
                capture_output=True,
                check=True,
                timeout=30,
            )
            try:
                server_dir = extract_immich_server(docker, container, version)
            finally:
                subprocess.run(
                    [docker, "rm", container], capture_output=True, timeout=10
                )
        except (RuntimeError, subprocess.SubprocessError, FileNotFoundError, OSError):
            # No local Docker — download directly from ghcr.io
            log.info("  No local Docker — downloading server from ghcr.io...")
            try:
                server_dir = download_immich_server(version)
            except RuntimeError as e:
                log.error("Download failed: %s", e)
                log.info(
                    "  Manual alternative: extract on your NAS and use --import-server"
                )
                return

    if server_dir is None:
        raise RuntimeError(
            "Server extraction failed. Use --import-server to provide server files."
        )

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
        log.info(
            "Edit it directly, or delete it and re-run --manual for a fresh template."
        )
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
    log.info(
        "Edit the config with your Immich connection details, then extract the server:"
    )
    log.info("")
    log.info("  # On the machine where Immich's Docker runs:")
    log.info(
        "  docker cp immich_server:/usr/src/app/server - | gzip > immich-server.tar.gz"
    )
    log.info("  docker cp immich_server:/build - | gzip > immich-build.tar.gz")
    log.info("")
    log.info("  # Copy to this Mac, then import:")
    log.info(
        "  python -m immich_accelerator setup --import-server ./immich-server.tar.gz"
    )
    log.info("")
    log.info("  # Then start:")
    log.info("  python -m immich_accelerator start")


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
        log.info("Server imported. Run: python -m immich_accelerator start")
    elif args.url:
        _setup_remote(args)
    else:
        _setup_local(args)


def _find_python() -> str | None:
    """Find Python 3.11+, or offer to install it."""
    # Check versioned binaries first
    for p in [
        "/opt/homebrew/bin/python3.11",
        "/usr/local/bin/python3.11",
        "/opt/homebrew/bin/python3.12",
        "/usr/local/bin/python3.12",
        "/opt/homebrew/bin/python3.13",
        "/usr/local/bin/python3.13",
    ]:
        if os.path.isfile(p):
            return p
    # Check system python3
    try:
        r = subprocess.run(
            ["python3", "--version"], capture_output=True, text=True, timeout=5
        )
        version = r.stdout.strip() + r.stderr.strip()  # some builds print to stderr
        import re

        m = re.search(r"3\.(\d+)", version)
        if m and int(m.group(1)) >= 11:
            return "python3"
    except (subprocess.SubprocessError, OSError):
        pass
    if _brew_install("python@3.11"):
        for p in ["/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.11"]:
            if os.path.isfile(p):
                return p
    return None


def _find_ml_dir() -> Path | None:
    """Find the immich-ml-metal service directory. Sets up venv if needed."""
    candidates = [
        Path(__file__).parent.parent / "ml",
        Path.home() / "immich-ml-metal",
    ]

    # Find a directory with ML source code
    ml_dir = None
    for d in candidates:
        if (d / "src" / "main.py").exists():
            ml_dir = d
            break
    if not ml_dir:
        return None

    # Check if venv already exists and works
    venv_python = ml_dir / "venv" / "bin" / "python3"
    if venv_python.exists():
        return ml_dir

    # Venv missing — offer to set it up
    log.info("ML service found at %s but venv is missing.", ml_dir)
    python = _find_python()
    if not python:
        log.warning("  Python 3.11+ not found. ML service won't be available.")
        log.warning("  Install with: brew install python@3.11")
        return None

    try:
        answer = input("  Set up ML service venv? [Y/n] ").strip().lower()
    except EOFError:
        return None
    if answer and answer != "y":
        return None

    log.info("  Creating venv with %s...", python)
    result = subprocess.run(
        [python, "-m", "venv", str(ml_dir / "venv")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        log.error("  Venv creation failed: %s", result.stderr[-300:])
        return None

    log.info("  Installing ML dependencies (this may take a few minutes)...")
    pip = str(ml_dir / "venv" / "bin" / "pip")
    req = ml_dir / "requirements.txt"
    if not req.exists():
        log.error("  requirements.txt not found in %s", ml_dir)
        return None

    result = subprocess.run(
        [pip, "install", "-r", str(req)], capture_output=False, timeout=600
    )
    if result.returncode != 0:
        log.error("  pip install failed")
        return None

    log.info("  ML service ready")
    return ml_dir


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
        result = subprocess.run(
            ["pgrep", "-f", "immich|src.main"],
            capture_output=True,
            text=True,
            timeout=5,
        )
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
        result = subprocess.run(
            ["pgrep", "-f", "ffmpeg-proxy/server.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
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
            log.error(
                "Docker is still running microservices. Two workers will conflict."
            )
            log.error("Set IMMICH_WORKERS_INCLUDE=api in docker-compose.yml first.")
            log.error("Run 'python -m immich_accelerator setup' for full instructions.")
            return
        if (
            config.get("upload_mount")
            and immich["media_location"] != config["upload_mount"]
        ):
            log.error(
                "IMMICH_MEDIA_LOCATION mismatch — Docker has '%s', we expect '%s'.",
                immich["media_location"] or "(not set)",
                config["upload_mount"],
            )
            log.error(
                "This WILL corrupt file paths in the database. Fix docker-compose.yml first."
            )
            return

        # Auto-update: if Docker image version changed, re-extract
        running_version = immich["version"].lstrip("v")
        cached_version = config.get("version", "").lstrip("v")
        if is_valid_version(immich["version"]) and running_version != cached_version:
            log.info(
                "Immich updated: %s -> %s. Re-extracting server...",
                cached_version,
                running_version,
            )
            server_dir = extract_immich_server(
                docker, immich["container"], immich["version"]
            )
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
    worker_env.update(
        {
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
        }
    )

    if config.get("upload_mount"):
        worker_env["IMMICH_MEDIA_LOCATION"] = config["upload_mount"]

    # /build link points to our build-data dir (set up during setup).
    # Required for Immich 2.7+ plugin WASM paths stored in the shared DB.
    build_data = DATA_DIR / "build-data"
    has_plugins = (build_data / "corePlugin" / "manifest.json").exists()

    if _build_link_ok():
        pass  # /build resolves correctly, both Docker and native see the same paths
    elif has_plugins:
        # Plugins exist but /build isn't set up — worker WILL fail on plugin load.
        # Try to set it up now (handles 2.6→2.7 upgrade case).
        if sys.stdin.isatty():
            _ensure_build_link()
        if not _build_link_ok():
            log.error("/build link is required for Immich 2.7+ but is not active.")
            log.error("  Run: immich-accelerator setup")
            log.error("  Then reboot to activate the /build link.")
            return
    else:
        # Pre-2.7, no plugins — IMMICH_BUILD_DATA fallback is sufficient
        worker_env["IMMICH_BUILD_DATA"] = str(build_data)

    # Set up VideoToolbox ffmpeg wrapper.
    # Immich doesn't support videotoolbox as an accel option, so we put a
    # wrapper script earlier in PATH that remaps software encoders to
    # VideoToolbox hardware encoders (h264 → h264_videotoolbox, etc.)
    wrapper_dir = DATA_DIR / "bin"
    wrapper_src = Path(__file__).parent / "ffmpeg-wrapper.sh"
    if not config.get("ffmpeg_path"):
        log.warning("No ffmpeg configured — video transcoding and thumbnails may fail.")
        log.warning("  Re-run setup to download jellyfin-ffmpeg.")
    elif wrapper_src.exists():
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
        worker_env["PATH"] = (
            f"{wrapper_dir}:{Path(config['ffmpeg_path']).parent}:{worker_env['PATH']}"
        )
        worker_env["FFMPEG_PATH"] = str(wrapper_dst)
    elif config.get("ffmpeg_path"):
        worker_env["PATH"] = (
            str(Path(config["ffmpeg_path"]).parent) + ":" + worker_env["PATH"]
        )

    # Start ML service
    ml_started_here = False
    ml_pid = read_pid("ml")
    if not ml_pid and config.get("ml_dir"):
        ml_dir = Path(config["ml_dir"])
        ml_python = ml_dir / "venv" / "bin" / "python3"
        if ml_python.exists():
            log.info("Starting ML service...")
            try:
                ml_pid = start_service(
                    "ml",
                    [str(ml_python), "-m", "src.main"],
                    os.environ.copy(),
                    str(ml_dir),
                )
                ml_started_here = True
                log.info("  ML service running (PID %d)", ml_pid)
            except RuntimeError:
                log.warning("  ML service failed to start — CLIP/face/OCR unavailable")
    elif ml_pid:
        log.info("ML service already running (PID %d)", ml_pid)

    # Start native Immich microservices worker
    log.info("Starting Immich worker (version %s)...", config["version"])
    try:
        worker_pid = start_service(
            "worker", [node, "dist/main.js"], worker_env, server_dir
        )
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
    for name in ("worker", "ml", "dashboard"):
        if kill_pid(name):
            log.info("%s stopped", name.capitalize())
            stopped = True
    if not stopped:
        log.info("Nothing running")


def cmd_status(_args):
    worker_pid = read_pid("worker")
    ml_pid = read_pid("ml")

    if not worker_pid and not ml_pid:
        log.info("Not running")
        return

    log.info(
        "Worker:     %s", f"running (PID {worker_pid})" if worker_pid else "stopped"
    )
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

    log.info("Updated to %s. Run: python -m immich_accelerator start", running)


def cmd_watch(_args):
    """Monitor services and restart on crash. Detects Docker updates.

    Suitable for launchd KeepAlive — runs forever, checking every 30s.
    """
    log.info("Watching services (Ctrl+C to stop)...")

    # First ensure everything is running
    if not read_pid("worker") or not read_pid("ml"):
        log.info("Services not running, starting...")
        cmd_start(argparse.Namespace(force=True))

    # Start dashboard in background if not already running
    try:
        config = load_config()
        import urllib.request as _urlreq

        _urlreq.urlopen("http://localhost:8420/", timeout=2)
    except Exception:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        dash_log = open(LOG_DIR / "dashboard.log", "a")
        proc = subprocess.Popen(
            [sys.executable, "-m", __package__ or "immich_accelerator", "dashboard"],
            cwd=str(Path(__file__).parent.parent),
            stdout=dash_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        dash_log.close()
        write_pid("dashboard", proc.pid)
        log.info("Dashboard started: http://localhost:8420")

    # Warn if auto-update won't work for remote setups
    _watch_config = load_config()
    if _watch_config.get("immich_url") and not _watch_config.get("api_key"):
        log.warning("Auto-update disabled: immich_url is set but api_key is missing.")
        log.warning("  Add api_key to %s to enable version checking.", CONFIG_FILE)

    check_count = 0
    self_update_notified = False
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
                        pid = start_service(
                            "ml",
                            [str(ml_python), "-m", "src.main"],
                            os.environ.copy(),
                            str(ml_dir),
                        )
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

            # Every 5 min, check if Immich updated
            check_count += 1
            if check_count >= 10:
                check_count = 0
                try:
                    cached = config.get("version", "").lstrip("v")
                    running = None

                    # Try local Docker first, fall back to Immich API
                    try:
                        docker = find_docker()
                        immich = detect_immich(docker)
                        running = immich["version"].lstrip("v")
                    except RuntimeError:
                        immich_url = config.get("immich_url")
                        api_key = config.get("api_key")
                        if immich_url and api_key:
                            try:
                                info = _query_immich_api(immich_url, api_key)
                                running = info["version"].lstrip("v")
                            except RuntimeError:
                                pass

                    if running and is_valid_version(running) and running != cached:
                        log.info(
                            "Immich updated: %s -> %s. Restarting with new version...",
                            cached,
                            running,
                        )
                        cmd_stop(None)
                        # Re-extract server — try Docker, fall back to ghcr.io download
                        try:
                            docker = find_docker()
                            immich = detect_immich(docker)
                            server_dir = extract_immich_server(
                                docker, immich["container"], running
                            )
                        except RuntimeError:
                            server_dir = download_immich_server(running)
                        config["version"] = running
                        config["server_dir"] = str(server_dir)
                        save_config(config)
                        cmd_start(argparse.Namespace(force=True))
                except RuntimeError:
                    pass  # Mid-restart or network issue, try again next cycle

                # Check for accelerator self-update (once per watch session)
                if not self_update_notified:
                    try:
                        import urllib.request as _urlreq3

                        req = _urlreq3.Request(
                            "https://api.github.com/repos/epheterson/immich-apple-silicon/releases/latest",
                            headers={"Accept": "application/vnd.github.v3+json"},
                        )
                        latest = json.loads(_urlreq3.urlopen(req, timeout=10).read())
                        latest_ver = latest.get("tag_name", "").lstrip("v")
                        if latest_ver and latest_ver != __version__:
                            log.info(
                                "Accelerator update available: %s -> %s",
                                __version__,
                                latest_ver,
                            )
                            log.info("  brew upgrade immich-accelerator")
                            log.info("  or: git pull && immich-accelerator setup")
                        self_update_notified = True
                    except Exception:
                        self_update_notified = True  # Don't retry on failure

        except KeyboardInterrupt:
            log.info("Watch stopped")
            return


def cmd_dashboard(args):
    """Start the web dashboard."""
    config = load_config()
    import importlib

    dashboard_mod = importlib.import_module(".dashboard", package=__package__)
    log.info("Starting dashboard on port %d...", args.port)
    dashboard_mod.run_dashboard(config, port=args.port)


# --- Main ---


def cmd_uninstall(_args):
    """Remove services, data, and launchd config."""
    plist = Path.home() / "Library" / "LaunchAgents" / "com.immich.accelerator.plist"
    ml_venv = Path(__file__).parent.parent / "ml" / "venv"

    log.info("")
    log.info("This will remove:")
    log.info("  - Running services (worker, ML, dashboard)")
    if plist.exists():
        log.info("  - Launchd service (auto-start on login)")
    log.info("  - Accelerator data (~/.immich-accelerator)")
    if ml_venv.exists():
        log.info("  - ML venv (./ml/venv)")
    log.info("")
    log.info(
        "Your Immich data, Docker containers, and Homebrew packages are NOT affected."
    )
    log.info("")

    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return
    if answer != "y":
        log.info("Cancelled.")
        return

    # Stop services
    cmd_stop(None)

    # Kill dashboard
    try:
        result = subprocess.run(
            ["pgrep", "-f", "immich_accelerator.*dashboard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                os.kill(int(line.strip()), signal.SIGTERM)
    except (subprocess.SubprocessError, ValueError, OSError):
        pass

    # Unload and remove launchd plist
    if plist.exists():
        subprocess.run(
            ["launchctl", "unload", str(plist)], capture_output=True, timeout=10
        )
        plist.unlink()
        log.info("Launchd service removed")

    # Remove /build firmlink from synthetic.conf
    _remove_build_link()

    # Remove data directory
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
        log.info("Removed %s", DATA_DIR)

    # Remove ML venv
    if ml_venv.exists():
        shutil.rmtree(ml_venv)
        log.info("Removed ML venv")

    log.info("")
    log.info("Uninstalled. To restore Immich to stock:")
    log.info(
        "  Remove IMMICH_WORKERS_INCLUDE and port mappings from docker-compose.yml"
    )
    log.info("  docker compose up -d")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        prog="immich-accelerator",
        description="Immich Accelerator — native macOS microservices worker",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    setup_p = sub.add_parser("setup", help="Detect Immich, download server, configure")
    setup_p.add_argument("--url", help="Remote Immich URL (e.g. http://nas:2283)")
    setup_p.add_argument("--api-key", help="Immich API key (for remote setup)")
    setup_p.add_argument(
        "--manual",
        action="store_true",
        help="Create config template for manual editing",
    )
    setup_p.add_argument(
        "--import-server",
        metavar="DIR",
        help="Import server from extracted directory or tarball",
    )
    start_p = sub.add_parser("start", help="Start native worker + ML")
    start_p.add_argument("--force", action="store_true", help="Restart if running")
    sub.add_parser("stop", help="Stop native services")
    sub.add_parser("status", help="Show what's running")
    logs_p = sub.add_parser("logs", help="Tail service logs")
    logs_p.add_argument(
        "service", nargs="?", choices=["worker", "ml"], default="worker"
    )
    sub.add_parser("update", help="Update to match Immich version")
    sub.add_parser("watch", help="Monitor services, restart on crash (for launchd)")
    dash_p = sub.add_parser("dashboard", help="Web dashboard (http://localhost:8420)")
    dash_p.add_argument("--port", type=int, default=8420, help="Dashboard port")
    sub.add_parser("uninstall", help="Remove services, data, and launchd config")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        {
            "setup": cmd_setup,
            "start": cmd_start,
            "stop": cmd_stop,
            "status": cmd_status,
            "logs": cmd_logs,
            "update": cmd_update,
            "watch": cmd_watch,
            "dashboard": cmd_dashboard,
            "uninstall": cmd_uninstall,
        }[args.command](args)
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
