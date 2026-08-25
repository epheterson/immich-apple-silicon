"""Immich Accelerator — run Immich microservices natively on macOS.

Usage:
    python -m immich_accelerator setup     # detect Immich, checkout code, configure
    python -m immich_accelerator start     # start native worker + ML service
    python -m immich_accelerator stop      # stop native services
    python -m immich_accelerator status    # show what's running
"""

from __future__ import annotations

import argparse
import datetime
import contextlib
import ctypes
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import sys
import time
import uuid
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
# Advisory lock so a component toggle and the watcher cannot both run cmd_start.
LOCK_FILE = DATA_DIR / "start.lock"
LOG_DIR = DATA_DIR / "logs"
# Why the worker is deliberately not running. The watch loop owns this file;
# status and the menu bar read it so a paused worker reads as "the library is
# gone" instead of a bare "Stopped" the user cannot explain.
PAUSE_FILE = DATA_DIR / "paused.json"

# Records the package version the running worker was started with, so the watch
# loop can tell when a `brew upgrade` left the worker on stale code.
WORKER_VERSION_FILE = PID_DIR / "worker.version"

# A running `watch` keeps executing the OLD code in memory after `brew upgrade`
# swaps the Cellar. Reading VERSION through Homebrew's version-independent opt
# symlink lets it notice it's now stale and relaunch into the new code. Absent
# on non-Homebrew (git) installs, where __version__ is authoritative.
#
# Both prefixes, for the same reason _brew_path checks both: /usr/local is an
# x86 brew under Rosetta, a real configuration here. Hardcoding the Apple
# Silicon prefix left `watch` on those installs reading its own __version__
# forever, so it never saw an upgrade and never relaunched into the new code,
# which is the exact failure this lookup exists to prevent.
_OPT_VERSION_FILES = [
    Path(prefix) / "opt/immich-accelerator/libexec/VERSION"
    for prefix in ("/opt/homebrew", "/usr/local")
]


def _installed_version() -> str:
    """The version currently installed on disk (via the stable opt symlink),
    which can differ from __version__ when watch is running post-upgrade."""
    for path in _OPT_VERSION_FILES:
        try:
            return path.read_text().strip()
        except OSError:
            continue
    return __version__


# Service logs are opened in append mode and the worker can spew stack traces
# for unsupported files (e.g. videos mislabeled .heic) — left unmanaged a log
# grew to 10GB. Cap each log; the watch loop and start enforce it.
LOG_MAX_BYTES = 200 * 1024 * 1024  # rotate a service log once it exceeds 200 MB
LOG_KEEP_TAIL_LINES = 2000  # lines of recent context preserved across a rotate

# Node.js majors Immich 2.7.x + sharp@0.34.5 are known to work with.
# Immich pins engines.node=24.x; sharp's native addons break with
# NODE_MODULE_VERSION mismatches on node 25+. Homebrew's default
# `node` formula tracks mainline (currently 25.x), so we pin to the
# closest LTS available as a keg-only bottle (node@22). Raise this
# range when Immich bumps engines in a new major release AND sharp
# ships a prebuilt for the new node major.
SUPPORTED_NODE_MAJORS = (22, 24)

# How long the ML service may hold a live PID while answering nothing before
# reconcile_ml stops believing it and restarts it. Generous on purpose: a cold
# native start loads weights before it serves, and a first-use model fetch is
# gigabytes, so the grace has to clear the longest legitimate silence.
ML_UNRESPONSIVE_GRACE = 300.0

# How long the library has to stay unreachable before the worker is paused.
# A slow NAS and a missing one look identical to a single probe: the probe has
# a 10s timeout, and a Synology mid-backup or a Wi-Fi hiccup can exceed that
# while the library is perfectly fine. Pausing on the first failure would stop
# the worker mid-job and start it again 30s later, which is the thrash this
# whole mechanism exists to prevent. A remount is still attempted immediately,
# since that is cheap and may fix things before the grace ever expires.
MEDIA_UNREACHABLE_GRACE = 120.0
# (pid, monotonic time it first went quiet). The PID is half the state, not
# bookkeeping: a bare timestamp belongs to no particular process, so a service
# replaced between two ticks inherits the dead one's elapsed silence and gets
# killed seconds into its own cold start, on a stopwatch that was never about
# it.
_ml_unresponsive_since: tuple[int, float] | None = None
# Latches the "someone else owns our port" warning so it is said once, not
# every watch tick.
_ml_foreign_listener_warned = False


# --- Utility ---


SYNTHETIC_CONF = Path("/etc/synthetic.d/immich-accelerator")
# Pre-1.3.3 installs put the entry here instead, and uninstall still has to
# clean it out. A constant rather than two literals, so tests can point it
# somewhere harmless: it is a shared system file that other software writes to,
# and the code rewrites it through `sudo tee`.
LEGACY_SYNTHETIC_CONF = Path("/etc/synthetic.conf")


def _build_link_ok() -> bool:
    """Check if /build points to our build-data directory."""
    build_data = DATA_DIR / "build-data"
    target = Path("/build")
    try:
        return target.exists() and target.resolve() == build_data.resolve()
    except OSError:
        return False


def _synthetic_build_entry(build_data: Path) -> str:
    """The synthetic.d line that maps /build to our build-data directory.

    The target column has NO leading slash: synthetic.conf(5) resolves it
    relative to /, and that's the form Apple's own examples use.
    """
    return f"build\t{str(build_data).lstrip('/')}\n"


def _has_build_entry(content: str) -> bool:
    """True if synthetic.d content already declares the /build link.

    Matches on the entry NAME (first tab-separated column), not a substring —
    a foreign line whose target merely contains 'build' must not count as the
    /build link being configured. Existence of the file is not enough: a user
    can hand-edit /etc/synthetic.d/immich-accelerator (e.g. to add an upload
    path) and clobber or omit our build entry (issue #61).
    """
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split("\t", 1)[0].strip() == "build":
            return True
    return False


def _strip_build_entry(content: str) -> str:
    """Return `content` with our build entries removed, foreign lines kept.

    Matches the entry NAME column (consistent with _has_build_entry), so
    comments and unrelated entries the user added survive (issue #61).
    """
    return "".join(
        line
        for line in content.splitlines(keepends=True)
        if line.strip().split("\t", 1)[0].strip() != "build"
    )


def _read_synthetic_conf() -> str:
    """Read SYNTHETIC_CONF, falling back to sudo for a root-only-readable file.

    A tight root umask can leave /etc/synthetic.d unsearchable (750) or the
    file unreadable by the non-root user — a case this module's own create
    path acknowledges. A plain read would then return "", and rewriting from
    "" would clobber the user's foreign lines. The sudo fallback reads the
    real content so append/strip operations preserve those lines (issue #61).
    Callers should only invoke this once they're already prepared to sudo.
    """
    try:
        if SYNTHETIC_CONF.exists():
            return SYNTHETIC_CONF.read_text()
        return ""
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["sudo", "cat", str(SYNTHETIC_CONF)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.stdout if r.returncode == 0 else ""
    except subprocess.SubprocessError:
        return ""


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
            legacy = LEGACY_SYNTHETIC_CONF
            try:
                content = legacy.read_text() if legacy.exists() else ""
            except OSError:
                content = ""
            if _has_build_entry(content):
                entry = _synthetic_build_entry(build_data)
                try:
                    # Write new synthetic.d file first — only remove legacy if this succeeds
                    r1 = subprocess.run(
                        # install -d pins mode 755 (see note in main create path)
                        ["sudo", "install", "-d", "-m", "755", "/etc/synthetic.d"],
                        capture_output=True,
                        timeout=30,
                    )
                    if r1.returncode != 0:
                        raise OSError("mkdir failed")
                    r2 = subprocess.run(
                        ["sudo", "tee", str(SYNTHETIC_CONF)],
                        input=entry,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if r2.returncode != 0:
                        raise OSError("tee failed")
                    # New file written — now safe to clean legacy
                    new_content = _strip_build_entry(content)
                    if new_content.strip():
                        subprocess.run(
                            ["sudo", "tee", str(legacy)],
                            input=new_content,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    else:
                        subprocess.run(
                            ["sudo", "rm", str(legacy)],
                            capture_output=True,
                            timeout=10,
                        )
                    log.info("Migrated /build link to /etc/synthetic.d/")
                except (OSError, subprocess.SubprocessError):
                    pass  # Non-fatal, link still works from legacy location
        return True

    if Path("/build").exists():
        log.warning("/build exists but doesn't point to our build-data.")
        log.warning("  Plugin paths may not resolve correctly.")
        return False

    # Read any existing synthetic.d file. Existence alone is NOT proof the
    # build link is configured — a user may have hand-edited this file (e.g.
    # to add a split-deployment upload path) and left out our build entry
    # (issue #61). Gate on the actual entry, and preserve foreign lines so
    # we never silently delete a user's manual additions. This first read is
    # best-effort and non-sudo (so we don't prompt for a password just to
    # check); a tight-umask file is re-read with sudo after the user opts in.
    existing = ""
    try:
        if SYNTHETIC_CONF.exists():
            existing = SYNTHETIC_CONF.read_text()
    except OSError:
        existing = ""

    if _has_build_entry(existing):
        # build entry present but /build not yet active → just needs a reboot.
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

    # Write our file in /etc/synthetic.d/ (avoids touching shared synthetic.conf).
    # Append-if-missing: keep any foreign lines the user added so we don't clobber
    # a manual split-deployment entry, but make sure our build line is present.
    # Re-read now (with a sudo fallback) so a root-only-readable file isn't seen
    # as empty — that would drop the user's foreign lines on the rewrite below.
    existing = _read_synthetic_conf()
    if _has_build_entry(existing):
        # Became readable and already has our entry — nothing to write.
        log.info("/build link already configured. Reboot to activate it.")
        return False
    foreign = [
        line
        for line in existing.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if foreign:
        log.warning(
            "%s already has %d entr%s we didn't write — preserving them:",
            SYNTHETIC_CONF,
            len(foreign),
            "y" if len(foreign) == 1 else "ies",
        )
        for line in foreign:
            log.warning("     %s", line)
    base = existing.rstrip("\n") + "\n" if existing.strip() else ""
    entry = base + _synthetic_build_entry(build_data)
    try:
        result = subprocess.run(
            # install -d, not mkdir -p: pin mode 755 so a tight root umask
            # (e.g. 027 → 750) can't leave /etc/synthetic.d unsearchable by
            # the non-root user. A 750 dir makes a later non-root exists()
            # check on its contents raise PermissionError. install -d also
            # normalises an existing dir's mode, self-healing prior installs.
            ["sudo", "install", "-d", "-m", "755", "/etc/synthetic.d"],
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

    # Remove our build entry from synthetic.d (v1.3.3+). Strip only the build
    # line and keep any foreign lines the user added (e.g. a split-deployment
    # upload path, issue #61) — rm the file only when nothing else remains.
    conf_content = _read_synthetic_conf()
    if conf_content:
        log.info("Removing /build link (requires sudo)...")
        remainder = _strip_build_entry(conf_content)
        try:
            if remainder.strip():
                result = subprocess.run(
                    ["sudo", "tee", str(SYNTHETIC_CONF)],
                    input=remainder,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            else:
                result = subprocess.run(
                    ["sudo", "rm", str(SYNTHETIC_CONF)],
                    capture_output=True,
                    timeout=10,
                )
            if result.returncode == 0:
                removed = True
            else:
                log.warning("  Could not update %s", SYNTHETIC_CONF)
        except subprocess.SubprocessError as e:
            log.warning("  Could not update %s: %s", SYNTHETIC_CONF, e)

    # Also clean legacy entry from /etc/synthetic.conf (pre-v1.3.3)
    legacy_conf = LEGACY_SYNTHETIC_CONF
    if legacy_conf.exists():
        try:
            content = legacy_conf.read_text()
            has_legacy = any(
                line.startswith("build\t") for line in content.splitlines()
            )
            if has_legacy:
                lines = [
                    line
                    for line in content.splitlines(keepends=True)
                    if not line.startswith("build\t")
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


def _rmtree_or_explain(path: Path, *, what: str) -> bool:
    """Remove a directory tree, or stop and explain — never force-delete.

    A container that previously ran as root can leave root-owned files in
    a bind-mounted directory, which makes shutil.rmtree fail partway with
    a PermissionError. We deliberately do NOT chmod or `sudo rm -rf` our
    way through it: if a path was ever mis-set (say a library someone
    created directly in their home dir), a force-delete could wipe real
    data. We err on the side of caution — report exactly what could not be
    removed, suggest the manual command, and let the user decide.

    Returns True only if the tree is now gone.
    """
    if not path.exists():
        return True
    try:
        shutil.rmtree(path)
        return True
    except OSError as e:
        log.error("")
        log.error("Could not fully remove %s (%s).", path, what)
        log.error("  %s: %s", type(e).__name__, e)
        log.error("  This usually means it holds files owned by root, left")
        log.error("  behind by a container that ran as root. Nothing was")
        log.error("  force-deleted — some files may remain.")
        log.error("  Review the contents, and if you're certain it's safe:")
        log.error("      sudo rm -rf %s", path)
        return False


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
            os.path.expanduser("~/.orbstack/bin/docker"),
            "/usr/local/bin/docker",
            "/opt/homebrew/bin/docker",
            "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
        ],
        "Install Docker Desktop or OrbStack.",
    )


def _docker_is_running(docker: str) -> bool:
    """Report whether this Docker binary can reach a daemon.

    A stopped OrbStack does not always refuse: it can wait for its daemon
    instead, so readiness has to be judged on the timeout as well as on the
    exit status.
    """
    try:
        result = subprocess.run([docker, "info"], capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _find_running_docker() -> str:
    """find_docker(), restricted to an installation that answers.

    find_docker() only checks that a binary exists, which is what the setup
    path wants: _ensure_docker_running() starts a stopped daemon. Every other
    caller reads live Docker state and has a fallback for not having it, so
    they want a daemon that is already up.

    This validates the same binary find_docker() picked rather than moving on
    to the next candidate. Falling through to another engine would swap which
    Immich stack we inspect, which is a surprise, not a recovery.
    """
    docker = find_docker()
    if not _docker_is_running(docker):
        raise RuntimeError(
            f"Docker is installed at {docker}, but its daemon is not running"
        )
    return docker


def _node_major_version(node_path: str) -> int | None:
    """Return the major version integer of a node binary, or None.

    Used by find_node() to filter brew-installed nodes to only those
    Immich + sharp will accept. We never trust the path name — only
    what `--version` actually reports.
    """
    try:
        result = subprocess.run(
            [node_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.match(r"v(\d+)\.", result.stdout.strip())
    return int(match.group(1)) if match else None


def find_node() -> str:
    """Return a node binary whose major version is in SUPPORTED_NODE_MAJORS.

    Homebrew's default `node` formula tracks the current mainline
    (25.x as of 2026-04), which breaks sharp's native addons with
    NODE_MODULE_VERSION mismatches. We prefer the keg-only LTS
    node@22 (closest available bottle) first, fall through to any
    other keg-only formula we might add later, and only accept
    /opt/homebrew/bin/node if its actual reported version is in the
    supported range.

    If nothing compatible is present, install node@22 via Homebrew.
    """
    keg_candidates = [
        f"/opt/homebrew/opt/node@{major}/bin/node" for major in SUPPORTED_NODE_MAJORS
    ]
    fallback_candidates = ["/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for p in keg_candidates:
        if os.path.isfile(p):
            return p
    for p in fallback_candidates:
        if os.path.isfile(p):
            major = _node_major_version(p)
            if major is not None and major in SUPPORTED_NODE_MAJORS:
                return p
    # Nothing compatible — install the closest LTS we support.
    if _brew_install("node@22"):
        p = "/opt/homebrew/opt/node@22/bin/node"
        if os.path.isfile(p):
            return p
    raise RuntimeError(
        "Node.js (version 22 or 24) not found. " "Install with: brew install node@22"
    )


def find_npm() -> str:
    """Return the npm binary colocated with the node we picked.

    If find_node() returned a keg-only node@XX build, npm lives in
    the same opt dir and won't be on PATH under /opt/homebrew/bin.
    Prefer the colocated one so `npm rebuild` picks up the matching
    node.
    """
    try:
        node_path = find_node()
        npm_colocated = str(Path(node_path).parent / "npm")
        if os.path.isfile(npm_colocated):
            return npm_colocated
    except RuntimeError:
        pass
    return find_binary(
        "npm",
        ["/opt/homebrew/bin/npm", "/usr/local/bin/npm"],
        "Install with: brew install node@22",
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


def _docker_capture(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a docker command for detection, reporting a hang as RuntimeError.

    Every detection call fails the same way: we could not read local Docker
    state. A daemon that stops between _find_running_docker() and here leaves
    the CLI waiting, and callers only catch RuntimeError.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Docker at {argv[0]} stopped responding") from e


def detect_immich(docker: str) -> dict:
    """Detect running Immich instance from Docker."""
    result = _docker_capture([docker, "ps", "--format", "{{.Names}}\t{{.Image}}"], 10)
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
    version_result = _docker_capture(
        [docker, "exec", server_container, "cat", "/usr/src/app/server/package.json"],
        10,
    )
    if version_result.returncode == 0:
        try:
            version = json.loads(version_result.stdout)["version"]
        except (json.JSONDecodeError, KeyError):
            pass

    if not is_valid_version(version):
        inspect = _docker_capture(
            [docker, "inspect", server_container, "--format", "{{.Config.Image}}"], 10
        )
        if inspect.returncode == 0:
            tag = inspect.stdout.strip().split(":")[-1]
            if is_valid_version(tag):
                version = tag

    # Get env vars
    env_result = _docker_capture([docker, "exec", server_container, "env"], 10)
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

    # Find which mount holds the upload library the way Immich itself does
    # (services/storage.service.js detectMediaLocation): an explicit
    # IMMICH_MEDIA_LOCATION wins; otherwise Immich uses whichever of /data or
    # /usr/src/app/upload exists in the container. The old detector hardcoded a
    # "/upload" substring match, so it missed the modern default where uploads
    # are bind-mounted at /data (issue #62).
    #
    # Only bind mounts qualify: the native worker reads files directly off disk,
    # so a named/anonymous volume (Source lives inside Docker's VM) is no use.
    bind_dests = {}
    for m in mounts:
        if m.get("Type") and m.get("Type") != "bind":
            continue
        d = m.get("Destination", "").rstrip("/")
        src = m.get("Source", "")
        if d and src:
            bind_dests.setdefault(d, src)

    media_env = env.get("IMMICH_MEDIA_LOCATION", "").rstrip("/")
    media_dest = media_env if media_env in bind_dests else ""
    if not media_dest:
        for candidate in ("/data", "/usr/src/app/upload"):
            if candidate in bind_dests:
                media_dest = candidate
                break

    upload_mount = bind_dests.get(media_dest)
    if not upload_mount:
        # Last resort: legacy substring match for non-standard destinations.
        for d, src in bind_dests.items():
            if "/upload" in d:
                upload_mount = src
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
        result = _docker_capture([docker, "port", name, default], 5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split(":")[-1]
    return default


# --- Environment health checks ---


def _preflight_env_health(config: dict) -> bool:
    """Auto-detect and fix common environment issues before starting.

    Each check is non-fatal — we log a warning and attempt to fix.
    If the fix fails, we warn but don't block startup. The worker
    will hit the issue at runtime and the user will see the error
    in context, which is better than a cryptic preflight failure.

    Checks added here should be things we've seen break in the
    wild and can fix without user intervention.
    """
    brew = shutil.which("brew") or "/opt/homebrew/bin/brew"

    # ImageMagick HEIC codec — Immich uses ImageMagick for person
    # face thumbnails (not Sharp). If the HEIC codec module is
    # missing, PersonGenerateThumbnail fails on HEIC-originating
    # faces. brew reinstall fixes it.
    identify = shutil.which("identify")
    if identify:
        try:
            result = subprocess.run(
                [identify, "-list", "format"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "HEIC" not in result.stdout:
                log.warning("ImageMagick HEIC codec missing — reinstalling...")
                fix = subprocess.run(
                    [brew, "reinstall", "imagemagick"],
                    capture_output=True,
                    timeout=300,
                )
                if fix.returncode == 0:
                    log.info("  ImageMagick reinstalled")
                else:
                    log.warning("  brew reinstall failed (exit %d)", fix.returncode)
        except (subprocess.SubprocessError, OSError):
            pass

    # NFS mount reachable — for split setups where upload_mount
    # is on a network share (e.g., /nas/...). If the mount went
    # stale (NAS rebooted, network blip), the worker will hang
    # on first file access. Use a short timeout via a subprocess
    # stat call instead of Path.exists() which can hang indefinitely
    # on a stale NFS mount.
    upload_mount = config.get("upload_mount", "")
    if upload_mount and not upload_mount.startswith(("/Users", "/tmp")):
        try:
            probe = subprocess.run(
                ["stat", upload_mount],
                capture_output=True,
                timeout=5,
            )
            if probe.returncode != 0:
                log.warning(
                    "upload_mount %s is not accessible — check NFS/SMB mount.",
                    upload_mount,
                )
            # In a child, with a timeout, for the same reason the stat above is:
            # os.access() is a bare access(2) in this process, and on a hung
            # mount that call never returns. This one wedged the release Mac's
            # watcher indefinitely while it held the start lock, so every later
            # start blocked behind it too, and the timeout above bought nothing
            # because the very next line reintroduced the hang it prevented.
            elif (
                subprocess.run(["/bin/test", "-w", upload_mount], timeout=5).returncode
                != 0
            ):
                log.warning(
                    "upload_mount %s is not writable — thumbnails will fail.",
                    upload_mount,
                )
        except subprocess.TimeoutExpired:
            log.warning(
                "upload_mount %s timed out — NFS/SMB mount may be stale.",
                upload_mount,
            )
        except OSError as e:
            log.warning("upload_mount %s: %s", upload_mount, e)

    # DB connectivity — try a real psql query, not just TCP connect.
    # ECONNRESET from Postgres looks identical to "unreachable" from
    # the worker's perspective. A real query surfaces auth failures,
    # SSL issues, pg_hba rejections, and port conflicts clearly (#42).
    db_host = config.get("db_hostname", "localhost")
    db_port = config.get("db_port", "5432")
    db_user = config.get("db_username", "postgres")
    db_name = config.get("db_name", "immich")
    db_pass = config.get("db_password", "")
    psql = shutil.which("psql") or "/opt/homebrew/opt/libpq/bin/psql"
    if Path(psql).exists():
        try:
            result = subprocess.run(
                [
                    psql,
                    "-h",
                    db_host,
                    "-p",
                    str(db_port),
                    "-U",
                    db_user,
                    "-d",
                    db_name,
                    "-c",
                    "SELECT 1",
                    "-t",
                    "-A",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env={**os.environ, "PGPASSWORD": db_pass},
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                log.error("Postgres connection failed:")
                log.error(
                    "  host=%s port=%s user=%s db=%s",
                    db_host,
                    db_port,
                    db_user,
                    db_name,
                )
                if "Connection reset" in err or "ECONNRESET" in err:
                    log.error(
                        "  Connection was reset — port conflict or auth rejection."
                    )
                    log.error("  Is another service using port %s?", db_port)
                    log.error(
                        "  Does docker-compose expose the port without 127.0.0.1 prefix?"
                    )
                elif "password authentication failed" in err:
                    log.error(
                        "  Password rejected. Check DB_PASSWORD matches config.json."
                    )
                elif "Connection refused" in err:
                    log.error(
                        "  Nothing listening on %s:%s. Is the database running?",
                        db_host,
                        db_port,
                    )
                else:
                    log.error("  %s", err.split("\n")[0] if err else "unknown error")
                log.error("")
                log.error(
                    "  Worker cannot start without a working database connection."
                )
                return False  # Block startup — worker will crash anyway
        except subprocess.TimeoutExpired:
            log.warning(
                "Postgres connection timed out (host=%s port=%s)", db_host, db_port
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        # No psql available — fall back to TCP connect check
        try:
            with socket.create_connection((db_host, int(db_port)), timeout=3):
                pass
        except (OSError, ValueError):
            log.error("Postgres at %s:%s is unreachable.", db_host, db_port)
            log.error("  Is the database container running? Are ports exposed?")
            return False  # Block startup

    # Redis connectivity — try a real PING if redis-cli is available.
    redis_host = config.get("redis_hostname", "localhost")
    redis_port = config.get("redis_port", "6379")
    redis_username = config.get("redis_username", "")
    redis_password = config.get("redis_password", "")
    redis_cli = shutil.which("redis-cli")
    if redis_cli:
        try:
            # REDISCLI_AUTH keeps the password off the process list.
            redis_env = os.environ.copy()
            if redis_password:
                redis_env["REDISCLI_AUTH"] = redis_password
            redis_cmd = [redis_cli, "-h", redis_host, "-p", str(redis_port)]
            if redis_username:
                redis_cmd += ["--user", redis_username]
            redis_cmd.append("PING")
            result = subprocess.run(
                redis_cmd,
                capture_output=True,
                text=True,
                timeout=5,
                env=redis_env,
            )
            if "PONG" not in (result.stdout or ""):
                err = (result.stderr or result.stdout or "").strip()
                log.error(
                    "Redis connection failed (host=%s port=%s):", redis_host, redis_port
                )
                log.error("  %s", err[:200] if err else "no response")
                log.error("  Worker needs Redis for the job queue.")
                return False
        except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
            try:
                with socket.create_connection((redis_host, int(redis_port)), timeout=3):
                    pass
            except (OSError, ValueError):
                log.error("Redis at %s:%s is unreachable.", redis_host, redis_port)
                return False
    else:
        try:
            with socket.create_connection((redis_host, int(redis_port)), timeout=3):
                pass
        except (OSError, ValueError):
            log.error("Redis at %s:%s is unreachable.", redis_host, redis_port)
            return False  # Block startup

    # Media location subdirectory check — Immich expects these under
    # IMMICH_MEDIA_LOCATION. If they're missing, the path is wrong or
    # the Docker volume mount is incomplete. Don't auto-create — that
    # would hide the real problem (#43).
    if upload_mount and Path(upload_mount).exists():
        expected = [
            "upload",
            "thumbs",
            "encoded-video",
            "library",
            "profile",
            "backups",
        ]
        missing = [d for d in expected if not Path(upload_mount, d).exists()]
        if missing:
            log.error(
                "IMMICH_MEDIA_LOCATION (%s) is missing: %s",
                upload_mount,
                ", ".join(missing),
            )
            log.error("")
            log.error(
                "  This directory should contain: upload/, thumbs/, encoded-video/,"
            )
            log.error("  library/, profile/, backups/")
            log.error("")
            log.error("  Common causes:")
            log.error("    - IMMICH_MEDIA_LOCATION points to the wrong directory")
            log.error(
                "    - Docker volume mount only maps a subdirectory (e.g., upload/)"
            )
            log.error("      instead of the whole media location")
            log.error("")
            log.error("  Check your docker-compose volumes: the mount should cover")
            log.error("  the entire IMMICH_MEDIA_LOCATION, not just upload/ inside it.")
            return False

    return True


# --- Server management ---


def _rebuild_sharp(server_dir: Path) -> None:
    """Install Sharp's pre-built darwin-arm64 binary.

    The Docker image has linux Sharp binaries that can't run on macOS.
    We install the official pre-built darwin-arm64 package from npm,
    which bundles its own libvips (8.17.x). This matches what stock
    Immich Docker ships and avoids the UHDR auto-detect bug in system
    vips 8.18+ (#44): Homebrew's libvips has a UHDR loader that claims
    JPEG files but fails through Sharp's auto-detect chain. The
    pre-built vips doesn't include libultrahdr, so jpegload handles
    all JPEGs cleanly.

    Previous approach (npm rebuild / build_from_source) always compiled
    from source against system vips because the Docker extraction never
    included the darwin prebuilt. That was never intentional — we just
    didn't realize npm rebuild had nothing to fall back to.
    """
    npm = find_npm()
    sharp_dirs = list(server_dir.glob("node_modules/.pnpm/sharp@*/node_modules/sharp"))
    if not sharp_dirs:
        raise RuntimeError(
            "Sharp not found under server_dir/node_modules/.pnpm/sharp@* — "
            "extraction may be incomplete. Re-run setup."
        )
    sharp_dir = sharp_dirs[0]

    # Extract the Sharp version from the pnpm path (sharp@0.34.5)
    sharp_version = sharp_dir.parent.parent.name.split("@")[-1]
    if not sharp_version or not sharp_version[0].isdigit():
        sharp_version = "0.34.5"  # fallback

    log.info("Installing Sharp pre-built binary for macOS (v%s)...", sharp_version)

    node_bin = str(Path(find_node()).parent)
    env = {
        **os.environ,
        "PATH": f"{node_bin}:/opt/homebrew/bin:{os.environ.get('PATH', '')}",
    }

    # Install the official pre-built darwin-arm64 package.
    result = subprocess.run(
        [npm, "install", f"@img/sharp-darwin-arm64@{sharp_version}", "--no-save"],
        cwd=str(sharp_dir),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "")[-600:]
        raise RuntimeError(
            f"Failed to install @img/sharp-darwin-arm64@{sharp_version}.\n"
            f"  Last output:\n    {tail}\n"
        )

    # Remove source-built binary if it exists, so Sharp picks up the
    # pre-built one. The source-built binary links against system vips
    # which has the UHDR loader bug.
    source_build = sharp_dir / "src" / "build"
    if source_build.exists():
        try:
            shutil.rmtree(source_build)
        except OSError as e:
            log.warning("Could not remove source-built Sharp: %s", e)

    log.info("  Sharp pre-built binary installed")


def _verify_sharp_loads(server_dir: str, node: str) -> tuple[bool, str]:
    """Run ``require('sharp')`` via node and return (ok, stderr_tail).

    This is the cheapest possible preflight for the class of bug
    where Sharp's native addon fails to load because of a node
    version bump. It catches it in <1s instead of letting the
    worker crash mid-Nest-bootstrap 10+ seconds in with a stack
    trace that looks like an Immich bug.
    """
    try:
        result = subprocess.run(
            [node, "-e", "require('sharp'); console.log('sharp-ok')"],
            cwd=server_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"spawn failed: {e}"
    if result.returncode == 0 and "sharp-ok" in result.stdout:
        return True, ""
    return False, (result.stderr or result.stdout or "")[-600:]


def _check_node_engines_compat(server_dir: Path | str, node: str) -> tuple[bool, str]:
    """Parse Immich's package.json engines.node and compare to `node`.

    Returns (ok, message). We don't enforce the exact pin Immich
    sets (``24.14.1``) — any supported LTS major in
    SUPPORTED_NODE_MAJORS is acceptable. The check exists to catch
    "user ran `brew upgrade`, node silently jumped to 25, sharp
    broke" — the most common drift pattern on existing installs.
    """
    server_dir = Path(server_dir)
    pkg_path = server_dir / "package.json"
    if not pkg_path.exists():
        return True, ""
    try:
        pkg = json.loads(pkg_path.read_text())
        engines = str(pkg.get("engines", {}).get("node", ""))
    except (OSError, json.JSONDecodeError):
        return True, ""
    actual_major = _node_major_version(node)
    if actual_major is None:
        return False, f"could not read `{node} --version`"
    if actual_major in SUPPORTED_NODE_MAJORS:
        return True, ""
    if engines:
        return False, (
            f"node {actual_major}.x is incompatible with Immich's "
            f"engines.node={engines} (accelerator supports "
            f"{SUPPORTED_NODE_MAJORS}). Install: brew install node@22"
        )
    return False, (
        f"node {actual_major}.x is outside the accelerator-supported "
        f"range {SUPPORTED_NODE_MAJORS}. Install: brew install node@22"
    )


def _ghcr_urlopen_with_retry(req, timeout: int = 300, max_attempts: int = 4):
    """urlopen wrapper that retries on ghcr.io rate-limit responses.

    Anonymous ghcr.io pulls are rate-limited per-IP and respond with
    HTTP 429. A single image fetch may issue 30+ requests (index +
    platform manifest + each layer blob), so one transient limit used
    to fail the whole run. Retry up to `max_attempts` times with
    exponential backoff + jitter, honoring Retry-After when present.

    503 is retried too (ghcr.io's usual way of signalling "busy").
    Any other HTTP error bubbles up immediately — no retry on 404.

    Module-level for testability: mocking a closure-defined _get was
    brittle, this is not.
    """
    import random
    import urllib.error
    import urllib.request

    for attempt in range(max_attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == max_attempts - 1:
                raise
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and str(retry_after).isdigit():
                delay = min(int(retry_after), 60)
            else:
                delay = (2**attempt) + random.random()
            log.warning(
                "  ghcr.io rate-limited (%d), sleeping %.1fs and retrying...",
                e.code,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")


def _needs_core_plugin(version: str) -> bool:
    """Immich 2.7+ ships a WASM corePlugin we must extract from the image.

    Parses `X.Y.Z` (or `vX.Y.Z`) and returns True when the version is 2.7
    or later. Unparseable versions default to True — safer to over-fetch
    than to silently omit plugin files and crash at runtime.
    """
    try:
        parts = version.lstrip("v").split(".")
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return True
    return (major, minor) >= (2, 7)


def _has_27_plugin(build_data: Path) -> bool:
    """2.7.x layout: build-data/corePlugin/manifest.json."""
    return (build_data / "corePlugin" / "manifest.json").exists()


def _has_30_plugin(build_data: Path) -> bool:
    """3.0.x layout: build-data/plugins/<name>/manifest.json + dist/plugin.wasm.

    3.0 splits the plugin across image layers: the manifest.json (at the plugin
    root) and dist/ land in separate COPY layers, so require BOTH.
    """
    plugins = build_data / "plugins"
    if not plugins.is_dir():
        return False
    return any(
        (p / "manifest.json").exists() and (p / "dist" / "plugin.wasm").exists()
        for p in plugins.iterdir()
        if p.is_dir()
    )


def _build_has_core_plugin(build_data: Path) -> bool:
    """True once the WASM core plugin is FULLY extracted into build-data, in
    either the 2.7 (corePlugin/) or 3.0 (plugins/<name>/) layout.

    Used as the layer-loop early-exit signal: keying only on dist/plugin.wasm
    stopped extraction after the wasm layer but before the manifest layer, so
    Immich could not import the plugin ("Failed to import plugin from /build/").
    """
    return _has_27_plugin(build_data) or _has_30_plugin(build_data)


def _build_has_core_plugin_for(build_data: Path, bare_version: str) -> bool:
    """Like _build_has_core_plugin, but only the layout matching this version's
    era counts.

    A 2.7 corePlugin/ left behind while serving a 3.0 version (or vice-versa)
    must NOT be mistaken for this version's plugin. When the major can't be
    parsed, fall back to accepting either layout.
    """
    try:
        major = int(bare_version.lstrip("v").split(".")[0])
    except (ValueError, IndexError):
        return _build_has_core_plugin(build_data)
    return _has_30_plugin(build_data) if major >= 3 else _has_27_plugin(build_data)


def _build_is_plugin_era(build_data: Path) -> bool:
    """True if build-data looks like a 2.7+ plugin build, even if the plugin is
    only partially extracted.

    Distinct from _build_has_core_plugin (which requires the plugin to be FULLY
    extractable): here we only ask "does this build need the /build link". A
    partially-extracted 3.0 plugin (dist/plugin.wasm but no manifest.json) still
    needs the link, and the worker should surface the clear "run setup" error
    rather than falling through to the pre-2.7 IMMICH_BUILD_DATA fallback. We
    require actual plugin content (a manifest or a wasm), so a stray empty
    plugins/<x>/ dir on a genuinely pre-2.7 build is not misread as plugin-era.
    """
    if (build_data / "corePlugin").is_dir():
        return True
    plugins = build_data / "plugins"
    if not plugins.is_dir():
        return False
    return any(
        (p / "manifest.json").exists() or (p / "dist" / "plugin.wasm").exists()
        for p in plugins.iterdir()
        if p.is_dir()
    )


# build-data is a SINGLE shared dir (mapped to /build), rewritten on every
# extraction, so it only ever reflects the last-extracted version. The server
# cache is per-version (server/<ver>), so the two can drift apart after a
# version switch (e.g. 2.7 <-> 3.0). Stamp build-data with the version that
# populated it so the per-version cache check can tell whether the shared
# build-data actually belongs to the version being served.
_BUILD_DATA_STAMP = ".accel-version"


def _build_data_version(build_data: Path) -> str | None:
    """Return the version that populated build-data, or None if unstamped.

    A corrupt/non-UTF8 stamp (UnicodeDecodeError is a ValueError) is treated as
    unstamped rather than propagating a raw traceback out of the cache gate.
    """
    try:
        return (build_data / _BUILD_DATA_STAMP).read_text().strip() or None
    except (OSError, ValueError):
        return None


def _stamp_build_data(build_data: Path, bare_version: str) -> None:
    """Record which version populated build-data. Only call once build-data is
    known-complete for that version (a stamp is a completeness claim)."""
    try:
        (build_data / _BUILD_DATA_STAMP).write_text(bare_version + "\n")
    except OSError as e:
        log.warning("Could not stamp build-data version: %s", e)


def _finalize_build_data(build_data: Path, bare_version: str) -> None:
    """After an extraction, stamp build-data with its version.

    For plugin-era versions we only stamp once the core plugin is fully present:
    a stamp means "this build-data is complete for <version>", so we must never
    stamp a plugin-less build-data (that would make the cache trust a broken
    install forever). If the plugin is missing we warn loudly and skip the
    stamp, so the next start re-extracts (self-heal) instead of silently
    serving a worker that cannot import the plugin.
    """
    if _needs_core_plugin(bare_version) and not _build_has_core_plugin(build_data):
        log.warning(
            "build-data for %s is missing the core plugin after extraction; "
            "leaving it unstamped so the next start re-extracts.",
            bare_version,
        )
        return
    _stamp_build_data(build_data, bare_version)


def _build_data_ready(bare_version: str) -> bool:
    """Is the shared build-data usable for this plugin-era version?

    True when the version stamp matches and the plugin is present, OR (legacy,
    unstamped build-data from before stamping / from an offline import) the
    plugin for THIS version's layout is fully present. In the legacy case we
    adopt the build-data by stamping it, so upgrading users don't eat a needless
    full re-download. A stamp for a DIFFERENT version is real drift and is
    rejected (re-extract). Adoption keys on the era-specific layout so a stale
    cross-era plugin is never adopted.
    """
    build_data = DATA_DIR / "build-data"
    stamp = _build_data_version(build_data)
    if stamp == bare_version:
        return _build_has_core_plugin(build_data)
    if stamp is None and _build_has_core_plugin_for(build_data, bare_version):
        _stamp_build_data(build_data, bare_version)
        return True
    return False


def _cached_server_if_current(server_dir: Path, bare_version: str) -> Path | None:
    """Return the cached server iff it is complete for this exact version.

    A cached server/<ver>/dist/main.js is necessary but not sufficient: for
    plugin-era versions the shared build-data must also belong to this version
    AND carry the core plugin. Guarding on the version stamp closes three holes:
    a stale cross-era plugin making a broken cache look complete; a genuinely
    complete cache being force re-downloaded on a mere build-data mismatch; and
    (because we only stamp complete build-data) re-extracting forever.
    """
    if not (server_dir.exists() and (server_dir / "dist" / "main.js").exists()):
        return None
    if _needs_core_plugin(bare_version) and not _build_data_ready(bare_version):
        log.info(
            "Cached server %s is missing/mismatched plugin data, re-extracting.",
            bare_version,
        )
        return None
    log.info("Using cached Immich server %s", bare_version)
    return server_dir


def _has_everything(
    version: str,
    found_server: bool,
    found_build: bool,
    has_core_plugin: bool,
) -> bool:
    """Decide whether we've extracted enough to stop processing layers.

    Pure function so the break logic can be unit-tested without mocking
    the registry. Previously a broken size-based shortcut here caused
    corePlugin (which lives in a small layer) to be skipped.
    """
    if not (found_server and found_build):
        return False
    if _needs_core_plugin(version):
        return has_core_plugin
    return True


def _prune_old_server_versions(current_bare: str) -> None:
    """Remove every server/<version> build except the current one.

    Each extracted Immich server is ~0.5GB, and until now nothing removed old
    ones, so a long-running install accumulated every version it had ever run
    (a real box had seven, ~3.3GB). Only the current build is kept: build-data
    is a single shared directory stamped for one version
    (_finalize_build_data / _cached_server_if_current), so a retained older
    build can't be served from cache anyway (a rollback re-downloads), making
    it dead weight rather than a usable rollback. Deleting everything else also
    clears any leftover <version>.staging dirs from interrupted extractions.

    Call this only after the worker is up on the current version, so the old
    (now-stopped) build is safe to delete. Never touches the current build; a
    failed delete (or an unstattable/vanishing entry) is logged, not fatal.
    """
    server_root = DATA_DIR / "server"
    if not server_root.is_dir():
        return
    try:
        entries = list(server_root.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_dir() or entry.name == current_bare:
                continue
            shutil.rmtree(entry)
            log.info("Pruned old server build: server/%s", entry.name)
        except OSError as e:
            log.warning("Could not prune server/%s: %s", entry.name, e)


def download_immich_server(version: str) -> Path:
    """Download Immich server directly from ghcr.io — no Docker needed.

    Fetches the container image layers from GitHub Container Registry,
    extracts the server and build data. Works without Docker installed.
    """
    import urllib.request as urlreq
    import tarfile

    bare_version = version.lstrip("v")
    server_dir = DATA_DIR / "server" / bare_version

    cached = _cached_server_if_current(server_dir, bare_version)
    if cached is not None:
        return cached

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
        return _ghcr_urlopen_with_retry(req)

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
    if not _rmtree_or_explain(build_data, what="stale build-data"):
        raise RuntimeError(f"Could not clear {build_data} — see message above.")
    build_data.mkdir(parents=True, exist_ok=True)

    # Download and extract layers containing server and build data.
    # Process layers largest-first because server + bulk build data live
    # in the biggest layers — most runs exit long before touching the
    # small trailing metadata layers. Never skip layers by size: the
    # corePlugin WASM sits in its own sub-megabyte COPY layer and would
    # be dropped, stranding Immich 2.7+ without plugin files.
    found_server = False
    found_build = False
    sorted_layers = list(enumerate(layers))
    sorted_layers.sort(key=lambda x: x[1]["size"], reverse=True)

    import io

    for i, layer in sorted_layers:
        size_mb = layer["size"] / 1024 / 1024
        has_core = _build_has_core_plugin(build_data)
        if _has_everything(bare_version, found_server, found_build, has_core):
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

    _finalize_build_data(build_data, bare_version)
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

    cached = _cached_server_if_current(server_dir, bare_version)
    if cached is not None:
        return cached

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

    # Extract build data (geodata, plugins, web assets). On a re-run this
    # dir already exists; if we can't clear it cleanly, stop with a clear
    # message rather than a raw traceback (and without force-deleting).
    if not _rmtree_or_explain(build_data, what="stale build-data"):
        raise RuntimeError(f"Could not clear {build_data} — see message above.")
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

    _finalize_build_data(build_data, bare_version)
    log.info("Immich server %s ready", bare_version)
    return server_dir


# --- Process management ---


def save_config(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp file + rename prevents corruption if interrupted
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
        # POSIX text files end in a newline. Without it every editor and diff
        # tool reports "\ No newline at end of file" on a file people are
        # expected to open and read.
        f.write("\n")
    os.chmod(tmp, 0o600)
    tmp.rename(CONFIG_FILE)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise RuntimeError("Not set up yet. Run: python -m immich_accelerator setup")
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _get_process_start_time(pid: int) -> str | None:
    """Get process start time via ps. Used to detect PID reuse.

    LC_ALL=C is required, not cosmetic: ps formats lstart through the
    caller's locale, so the same process reads back as "Sat Aug 22
    23:07:37 2026" under C/en_US but "Sat 22 Aug 23:07:37 2026" under
    en_AU/en_GB. Our writer and reader are frequently different processes
    with different locales — launchd (brew services) passes no LANG at
    all, while a Terminal session inherits the user's region — so an
    unpinned locale makes read_pid see a start-time mismatch, conclude
    the PID was reused, and delete a pidfile whose service is healthy.
    Downstream that reports a running service as stopped, makes stop a
    no-op that orphans it, and makes start try to spawn a duplicate.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
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


_WORKER_CMD_RE = re.compile(
    r"^immich\s*$"  # 2.7+: process.title = 'immich'
    r"|(?:^|/)node\b.*/dist/main\.js(?:\s|$)"  # pre-2.7: node .../dist/main.js
)


def _scan_worker_pids(exclude: set[int] | None = None) -> list[int]:
    """Return PIDs of live Immich worker processes found via ``ps``.

    Immich 2.7+ sets ``process.title='immich'``, so the recorded PID's
    parent may exit while child 'immich' processes keep running.

    *exclude* is an optional set of PIDs to skip (e.g. tracked PIDs).
    The current process is always excluded.
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    skip = {os.getpid()}
    if exclude:
        skip |= exclude

    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_str, cmdline = line.split(None, 1)
            pid = int(pid_str)
        except ValueError:
            continue
        if pid in skip:
            continue
        if _WORKER_CMD_RE.search(cmdline):
            pids.append(pid)
    return pids


def _find_live_worker_pid() -> int | None:
    """Return any one live Immich worker PID, or None."""
    pids = _scan_worker_pids()
    return pids[0] if pids else None


def _adopt_live_worker() -> int | None:
    """Find a live worker via ps scan and adopt it into the PID file."""
    pid = _find_live_worker_pid()
    if pid is not None:
        write_pid("worker", pid)
    return pid


def _same_start_time(current: str, stored: str) -> bool:
    """Do these two `ps` start times describe the same process?

    `current` is always the C form now that ps is pinned to LC_ALL=C:

        Tue Aug 25 09:12:50 2026

    `stored` is that too, unless the pidfile predates the pin, in which case it
    holds whatever the writer's locale produced. Across the 83 UTF-8 locales on
    this machine that includes day-first ordering, non-English month names, a
    month written as "8/25", dot-separated clocks, trailing dots on the day and
    year, and at least one locale with no month word at all.

    So this does not try to parse them. Every attempt to did the same thing:
    fixed the locales it was tested against and broke others, most sharply by
    reading Finnish "Mar" (marraskuu, November) as March and by rejecting
    locales whose format lacked a field it expected. Both directions land on
    the same failure, which is that read_pid deletes the pidfile of a healthy
    service and ml and dashboard have no adopt-live path to recover with.

    Instead it asks one question it can answer exactly: is `stored` in our own
    format? We know that format, because we write it.

      - Equal strings: the same process.
      - `stored` parses as the C form and differs: genuinely a different
        process, so the pidfile is stale. This is every pidfile written from
        1.16 onward.
      - `stored` is not the C form: written before the pin, by a locale we
        cannot read. Accept it and move on, and restamp the file in the C
        form so the next read is a strict one.

    The last case is the only give: a PID reused between that old write and
    this read is adopted. It is the behaviour every release before 1.16 had for
    these users anyway, the restamp in read_pid closes it after a single
    read, and it is a far smaller risk than stopping a healthy service on
    upgrade.
    """
    if current == stored:
        return True
    try:
        datetime.datetime.strptime(" ".join(stored.split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return True  # not our format: pre-pin, unreadable, accept once
    return False  # our format and different: a different process


def read_pid(name: str) -> int | None:
    pid_file = PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        if name == "worker":
            return _adopt_live_worker()
        return None
    try:
        lines = pid_file.read_text().strip().split("\n")
        pid = int(lines[0])
        os.kill(pid, 0)  # check if process exists
        # Verify start time matches to detect PID reuse
        if len(lines) > 1 and lines[1]:
            current_start = _get_process_start_time(pid)
            if current_start and not _same_start_time(current_start, lines[1]):
                log.debug("PID %d reused (start time mismatch), cleaning up", pid)
                pid_file.unlink(missing_ok=True)
                if name == "worker":
                    return _adopt_live_worker()
                return None
            if current_start and current_start != lines[1]:
                # Accepted on the strength of being unreadable rather than of
                # matching: a pidfile written before ps was pinned to LC_ALL=C.
                # Restamp it in the form we can actually compare, so the next
                # read is a strict one. Without this the file keeps its old
                # string until the service restarts, and every read in between
                # is another one that cannot detect PID reuse.
                with contextlib.suppress(OSError):
                    pid_file.write_text(f"{pid}\n{current_start}")
        return pid
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        if name == "worker":
            return _adopt_live_worker()
        return None


def _kill_all_worker_processes():
    """Kill all 'immich' processes (Immich 2.7+ sets process.title).

    Sends SIGTERM first, waits briefly, then escalates to SIGKILL for
    any survivors.
    """
    pids = _scan_worker_pids()
    if not pids:
        return

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    # Give orphans a moment to exit gracefully before escalating
    time.sleep(1)
    for pid in pids:
        try:
            os.kill(pid, 0)  # still alive?
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _group_leader_is_ours(pgid: int, name: str) -> bool:
    """Does this process group's leader look like a service of ours?

    ps only, so it stays off the filesystem, which is what lets this run in the
    watcher on a machine whose mount has gone away.
    """
    if not name:
        return False
    try:
        cmd = subprocess.run(
            ["/bin/ps", "-p", str(pgid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if name == "worker":
        return bool(_WORKER_CMD_RE.search(cmd))
    if name == "ml":
        return bool(_ML_CMD_RE.search(cmd))
    if name == "dashboard":
        return "immich_accelerator" in cmd and "dashboard" in cmd
    return False


def _signal_service(pid: int, sig: int, name: str = "") -> None:
    """Signal one tracked service, by group only where the group is ours.

    Services have children that must go with them: the worker runs several
    processes and spawns ffmpeg and exiftool, and a uvicorn engine can fork
    workers. start_service and start_dashboard pass start_new_session, so a
    service that owns its session is a session leader, and a session leader's
    process group can only be joined by its own descendants.

    An ML service started by the watcher is the exception: it shares the
    watcher's process group on purpose, so both tests below fail for it and it
    is signalled by pid. That is what we want: it has no children to sweep,
    and its group is the watcher's own.

    A pid can also be one we adopted rather than spawned — read_pid falls back
    to a process scan for the worker, adopt_live_ml and start_dashboard adopt a
    live listener. What separates the two is whether the process group's leader
    is the session leader: only setsid makes that true, so a process started
    from a shell leads its job's group but not the session, and that group is
    not ours to signal.
    """
    try:
        pgid = os.getpgid(pid)
        # Signalling a group reaches everything in it, so the group has to be
        # attributable to us before we do. Leader-is-session-leader is what
        # setsid produces, but it is also true of plenty of processes nobody
        # here started: measured on one Mac, 15 more processes satisfy it than
        # satisfy "is itself the leader", among them another supervisor and its
        # children. Adopted pids are the ones that reach this, and the worker
        # scan matches a worker-shaped process anywhere on the machine with no
        # ownership check, so an adopted stranger would have taken its whole
        # group with it.
        if not _group_leader_is_ours(pgid, name):
            raise OSError("group leader is not one of ours")
        # The group is ours when its leader is also the session leader, which is
        # the shape start_new_session produces and a shell job never has.
        #
        # Testing getsid(pid) == pid instead asks whether this pid is itself the
        # leader, which is false for the pid the worker adoption path hands back:
        # _find_live_worker_pid returns a child, not the leader. The group then
        # went unsignalled, and _kill_all_worker_processes only matches immich
        # and node, so ffmpeg and exiftool kept running and writing into the
        # library after "Worker stopped".
        if os.getsid(pid) == pgid:
            os.killpg(pgid, sig)
            return
    except OSError:
        pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def kill_pid(name: str) -> bool:
    pid = read_pid(name)
    if pid is None:
        return False
    _signal_service(pid, signal.SIGTERM, name)

    # Also kill any orphaned immich processes not in the same group
    if name == "worker":
        _kill_all_worker_processes()

    # Wait for exit
    for _ in range(50):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        _signal_service(pid, signal.SIGKILL, name)

    (PID_DIR / f"{name}.pid").unlink(missing_ok=True)
    return True


try:
    _LIBPROC = ctypes.CDLL("libproc.dylib", use_errno=True)
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int
    _LIBPROC.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
except OSError:
    _LIBPROC = None

_PROC_PIDLISTFDS = 1
_PROC_FDINFO_SIZE = 8  # sizeof(struct proc_fdinfo): int32 fd + uint32 fdtype


def _process_fd_count(pid: int) -> int | None:
    """Live open file-descriptor count for a pid via libproc, or None.

    Used by the watcher's fd-leak safety net (#89). proc_pidinfo(PROC_PIDLISTFDS)
    with a NULL buffer returns the fd-table CAPACITY, a high-water mark that
    never shrinks when fds close, so we must pass a real buffer and count the
    bytes actually written (live_nfds * sizeof(proc_fdinfo)). Two syscalls, no
    subprocess: unlike lsof it can't hang on a stalled mount, does no DNS on the
    worker's DB/Redis sockets, and stays fast even when the table is huge (the
    exact case we must catch).
    """
    if _LIBPROC is None:
        return None
    capacity = _LIBPROC.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
    if capacity <= 0:
        return None
    buf = ctypes.create_string_buffer(capacity)
    written = _LIBPROC.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buf, capacity)
    if written <= 0:
        return None
    return written // _PROC_FDINFO_SIZE


def _worker_fd_total() -> int | None:
    """Total open fds across all live Immich worker processes, or None.

    Immich 2.7+ runs several processes titled 'immich'; the file-handle leak
    (#89) can accumulate in any of them, not just the one tracked in worker.pid,
    so sum every worker process rather than measuring a single pid.
    """
    if _LIBPROC is None:
        return None  # can't count fds; skip the ps scan entirely
    pids = _scan_worker_pids()
    if not pids:
        return None
    total = 0
    counted = False
    for pid in pids:
        count = _process_fd_count(pid)
        if count is not None:
            total += count
            counted = True
    return total if counted else None


def cap_log(log_path: Path, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """Cap a service log in place once it exceeds max_bytes, preserving the last
    LOG_KEEP_TAIL_LINES lines of context. Returns True if it rotated.

    Must truncate the existing inode rather than rename it: the worker/ML hold
    these files open in append (O_APPEND) mode, so a rename would leave the open
    fd writing to the moved file while the new one stayed empty until restart.
    With O_APPEND the process's next write resumes at end-of-file, so truncating
    to a small tail is safe while it keeps logging. (A write landing between the
    truncate and the tail rewrite could garble one line — acceptable at the rare
    200MB-rotation boundary.)
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return False
        tail = b""
        try:
            with open(log_path, "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - 4 * 1024 * 1024))  # last ~4MB covers the tail
                tail = b"\n".join(f.read().split(b"\n")[-LOG_KEEP_TAIL_LINES:])
        except OSError:
            tail = b""
        with open(log_path, "r+b") as f:
            f.truncate(0)
            f.write(
                b"[immich-accelerator] log rotated (exceeded "
                + str(max_bytes // (1024 * 1024)).encode()
                + b" MB)\n"
            )
            if tail:
                f.write(tail)
                if not tail.endswith(b"\n"):
                    f.write(b"\n")
        return True
    except OSError:
        return False


def cap_service_logs() -> None:
    """Cap all known service logs. Cheap (a stat each) unless one is oversized."""
    for name in ("worker", "ml", "dashboard"):
        if cap_log(LOG_DIR / f"{name}.log"):
            log.info(
                "Rotated %s.log (exceeded %d MB)", name, LOG_MAX_BYTES // (1024 * 1024)
            )


# Worker bootstrap failures a plain restart won't fix. Each entry pairs log
# substrings (all must be present) with actionable guidance, so when the worker
# dies we can point the user at the cause instead of leaving a raw stack trace
# and a silent 30s crash-restart loop.
_WORKER_FATAL_HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("Unable to initialize reverse geocoding",),
        "Reverse-geocoding (geodata) import failed. On a fresh split deployment the "
        "accelerator is the first microservices worker to run, so it performs "
        "Immich's one-time geodata import — and that bulk insert can break over a "
        "network database connection (write EPIPE).\n"
        "  Fix: initialize geodata once on your Immich frontend — temporarily remove "
        "IMMICH_WORKERS_INCLUDE=api so its own worker runs, let it finish, then "
        "re-add the variable — and start the accelerator again.\n"
        "  Details: https://github.com/epheterson/immich-apple-silicon#split-deployment-nas--mac",
    ),
]


def diagnose_worker_log(log_path: Path) -> str | None:
    """Scan the tail of the worker log for a known unrecoverable-bootstrap
    signature and return actionable guidance, or None if nothing is recognized."""
    try:
        tail = log_path.read_text(errors="replace")[-20000:]
    except OSError:
        return None
    for needles, guidance in _WORKER_FATAL_HINTS:
        if all(n in tail for n in needles):
            return guidance
    return None


# Media-readiness gate. Confirms IMMICH_MEDIA_LOCATION is the real, mounted
# media root before the worker starts, so it can't write thumbnails into a local
# placeholder directory that a network mount later masks (silent data loss).
# Mount-agnostic by design: instead of inspecting the mount type, we drop a
# marker file holding a unique id in the media root on first successful start and
# verify it on every later start. Present + matching = the real root is here;
# missing = a placeholder, or the mount isn't up yet.
MEDIA_MARKER_NAME = ".immich-accelerator-media-id"

# The probe runs in a child process with a hard timeout: a blocked filesystem
# syscall on a stale/hung network mount can't be interrupted in-process, so we
# must be able to kill it. Modes:
#   verify <root> <id> -> exit 0 iff a matching marker is found in any candidate
#   init   <root> <id> -> write the marker into the first WRITABLE candidate,
#                         exit 0 iff it reads back correctly
#
# Candidates are the media root PLUS Immich's standard subdirs (upload, thumbs,
# …). Why not just the root: Immich only ever writes to the subdirs, not the
# root — a split setup can have a root-owned, non-writable media root (e.g. a
# `/data` synthetic link) while the subdirs are perfectly writable (#80). So we
# test writability where Immich actually writes. The root stays first in the
# list for back-compat with markers written by 1.5.16 (which used the root).
# Only EXISTING dirs are used (no spurious subdir creation), so a missing root
# (mount not up) still correctly fails.
_MEDIA_PROBE = r"""
import os, sys
mode, root, want = sys.argv[1], sys.argv[2], sys.argv[3]
MARKER = ".immich-accelerator-media-id"
SUBDIRS = ("upload", "library", "thumbs", "encoded-video", "profile", "backups")
cands = [root] + [os.path.join(root, s) for s in SUBDIRS]
# A subdir pointed at another disk (the documented way to put thumbs or
# encoded-video on a fast SSD, #115) is a symlink. If its target is missing or
# is not a directory, Immich fails every write there with ENOENT/ENOTDIR, and
# isdir() is False either way so it would silently drop out of the candidates
# below while the marker still verifies via another subdir. Fail loudly.
#
# Only the paths Immich writes constantly are worth blocking startup for.
# `profile` and `backups` are incidental, so a removable drive holding one of
# those must not take the whole accelerator down; they are reported by the
# caller as a warning instead.
CRITICAL = ("upload", "library", "thumbs", "encoded-video")
broken = [
    d for d in cands
    if os.path.islink(d) and not os.path.isdir(os.path.realpath(d))
]
fatal = [d for d in broken if d == root or os.path.basename(d) in CRITICAL]
if fatal:
    sys.stderr.write("dangling:" + ",".join(fatal))
    sys.exit(5)
if broken:
    sys.stderr.write("warn-dangling:" + ",".join(broken))
cands = [d for d in cands if os.path.isdir(d)]
try:
    if mode == "verify":
        for d in cands:
            try:
                with open(os.path.join(d, MARKER)) as f:
                    if f.read().strip() == want:
                        sys.exit(0)
            except OSError:
                continue
        sys.exit(3)  # no matching marker anywhere -> placeholder / not mounted
    else:  # init: write the marker into the first writable candidate
        for d in cands:
            p = os.path.join(d, MARKER)
            try:
                with open(p, "w") as f:
                    f.write(want)
                with open(p) as f:
                    if f.read().strip() == want:
                        sys.exit(0)
            except OSError:
                continue
        sys.exit(2)  # no writable candidate (perm problem / not mounted)
except Exception as e:
    sys.stderr.write(str(e))
    sys.exit(4)
"""


# Network filesystems we know how to remount. Local disks are deliberately
# excluded: if an APFS volume vanished, remounting is not our call to make.
_REMOUNTABLE_FS = {"smbfs", "nfs", "afpfs"}

# Backoff between remount attempts, in seconds. A NAS that is off overnight
# should not be probed every 30s, and an SMB server should never be hit with a
# tight retry loop; repeated failed auth is how accounts get locked.
_REMOUNT_BACKOFF = (0, 60, 300, 900)
# Watch cycles between advisory reads of the library. The loop runs every 30s,
# so ten cycles is five minutes.
MEDIA_IO_EVERY = 10


def mount_recipe_for(root: str) -> dict | None:
    """The mount that provides `root`, as something we could replay later.

    Parsed from /sbin/mount, whose lines read `<spec> on <point> (<fs>, ...)`.
    The spec keeps its URL encoding (`Time%20Machine`) because that is the form
    mount_smbfs wants back.

    Returns None for local disks and for a root that is not on its own mount:
    both mean there is nothing here we should be replaying.
    """
    try:
        out = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    # The mount table reports resolved paths (/private/tmp, not /tmp), so a
    # config path that goes through a symlink would match nothing.
    #
    # But resolve() lstats every component, and a component on a mount whose
    # server has gone away never returns: not a timeout, a wedge. This is called
    # from the watch loop, so it took the whole watcher down with the NAS on the
    # release Mac, at exactly the moment its job was to notice that. Resolving
    # is therefore a fallback, tried only when the plain path matches nothing,
    # and done where a hang cannot reach this process.
    candidates = [root]

    best = None
    for want in candidates:
        best = _best_mount_for(want, out)
        if best:
            return best
    # Resolving is only worth a child process if some remountable mount could
    # plausibly be the answer. A library on the internal disk matches nothing
    # here whatever the path resolves to, and that is most installs, so without
    # this the watcher forked an interpreter every cycle for a result it then
    # discarded.
    if not any(
        line.rpartition(" (")[2].split(",")[0].rstrip(")").strip() in _REMOUNTABLE_FS
        for line in out.splitlines()
    ):
        return None
    resolved = _resolve_offthread(root)
    return _best_mount_for(resolved, out) if resolved and resolved != root else None


def _resolve_offthread(path: str, timeout: int = 5) -> str | None:
    """Resolve symlinks in a child process, so a dead mount cannot wedge us.

    resolve() lstats every component. On a mount whose server has gone away
    that call does not time out, it never returns, and the caller is the watch
    loop.
    """
    try:
        r = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys,pathlib;print(pathlib.Path(sys.argv[1]).resolve())",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _best_mount_for(root: str, out: str) -> dict | None:
    """Longest mount in `out` that covers `root`, or None."""
    best = None
    for line in out.splitlines():
        head, sep, tail = line.rpartition(" (")
        if not sep or not head:
            continue
        fstype = tail.split(",")[0].rstrip(")").strip()
        if fstype not in _REMOUNTABLE_FS:
            continue
        # Split on the FIRST " on ": the spec (//user@host/share) does not
        # contain it, while mount points routinely do ("/Volumes/Time Machine").
        spec, sep, point = head.partition(" on ")
        if not sep:
            continue
        try:
            covers = Path(root) == Path(point) or Path(point) in Path(root).parents
        except (OSError, ValueError):
            continue
        # Longest match wins, so a share mounted inside another share is
        # recorded as itself rather than as its parent.
        if covers and (best is None or len(point) > len(best["mountpoint"])):
            best = {"fstype": fstype, "spec": spec.strip(), "mountpoint": point}
    return best


def is_mounted(point: str) -> bool:
    """Is something already mounted at this exact path?

    macOS will happily stack a second mount on an occupied mount point, and the
    probe can report a healthy mount as unreachable when the server is merely
    slow. Without this check, one slow NAS could leave the path buried under a
    pile of mounts that only a reboot fully unwinds.
    """
    try:
        out = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines():
        head, sep, _ = line.rpartition(" (")
        if not sep:
            continue
        _, sep, existing = head.partition(" on ")
        if sep and existing.strip() == point:
            return True
    return False


def remount(recipe: dict) -> tuple[bool, str]:
    """Replay a recorded mount. Returns (ok, reason).

    reason is "auth" when the server rejected our credentials, which the caller
    must treat as terminal: retrying a bad password in a loop locks accounts.

    -N on mount_smbfs means "never prompt". There is no terminal here, so a
    prompt would hang the watcher forever; with -N the credentials come from the
    login keychain, which is where they already are if the share was ever
    mounted in Finder with "remember this password".
    """
    fstype, spec, point = recipe["fstype"], recipe["spec"], recipe["mountpoint"]
    if fstype == "smbfs":
        cmd = ["/sbin/mount_smbfs", "-N", spec, point]
    elif fstype == "nfs":
        cmd = ["/sbin/mount_nfs", spec, point]
    else:
        return False, f"no non-interactive remount for {fstype}"

    try:
        Path(point).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"cannot create mount point: {exc}"

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "mount timed out"
    except OSError as exc:
        return False, str(exc)

    if proc.returncode == 0:
        return True, ""
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = err[-1] if err else f"exit {proc.returncode}"
    lowered = detail.lower()
    if any(
        k in lowered
        for k in ("authentication", "permission denied", "not permitted", "password")
    ):
        return False, "auth"
    return False, detail


def _media_probe(
    mode: str, root: str, media_id: str, timeout: int = 15
) -> tuple[bool, list[str], str]:
    """Run the marker probe in a child process with a hard timeout. Returns
    (ok, dangling, detail): ok is True only on a clean exit 0; any nonzero exit
    or timeout (e.g. a hung network mount) is not-ready. `dangling` lists media
    subdirs that are symlinks to a missing target (exit 5, see #115). `detail`
    is why it failed.

    detail exists because it once did not. This returned a bare False, so when
    the probe started failing inside the running service while succeeding from
    a shell on the same machine, the reason had already been discarded and
    there was nothing to debug from.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", _MEDIA_PROBE, mode, root, media_id],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        err = (result.stderr or "").strip()
        if result.returncode == 5:
            paths = err.split("dangling:", 1)[-1] if "dangling:" in err else ""
            return False, [p for p in paths.split(",") if p], err or "dangling symlink"
        # A broken link on a non-critical subdir (profile/backups) is worth
        # saying out loud, but not worth refusing to start over.
        if "warn-dangling:" in err:
            for p in err.split("warn-dangling:", 1)[-1].split(","):
                if p:
                    log.warning(
                        "Media folder %s is a broken symlink; jobs "
                        "writing there will fail.",
                        p,
                    )
        if result.returncode == 0:
            return True, [], ""
        return False, [], err or f"probe exited {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, [], f"probe timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as exc:
        return False, [], f"could not run the probe: {exc}"


def set_paused(reason: str, detail: str = "") -> None:
    """Record why the worker is down, or clear it when reason is empty."""
    try:
        if not reason:
            PAUSE_FILE.unlink(missing_ok=True)
            return
        PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.write_text(
            json.dumps({"reason": reason, "detail": detail, "since": time.time()})
        )
    except OSError:
        pass  # a status nicety; never worth failing the loop over


def read_paused() -> dict | None:
    try:
        return json.loads(PAUSE_FILE.read_text())
    except (OSError, ValueError):
        return None


def _mount_covers(point: str, root: str) -> bool:
    """Is `point` the mount that would provide `root`: the path itself, or an
    ancestor of it?"""
    if not point or not root:
        return False
    try:
        return Path(root) == Path(point) or Path(point) in Path(root).parents
    except (OSError, ValueError):
        return False


def library_mount(config: dict) -> str:
    """The mount point recorded as serving this library, or "" if there is none
    we can trust.

    Only the recipe recorded while the library was last healthy may speak for
    it, and only while it still covers the configured path. Both halves of that
    matter, and review of the first version of this fix found each of them:

    A recipe is written in one place and never cleared, and mount_recipe_for
    returns None for a local disk, so the refresh cannot overwrite a stale one.
    A library moved from a NAS to a local disk would otherwise keep pointing at
    the old share and be paused against a mount it no longer uses, with no way
    back.

    And an ancestor is not a substitute. Asking the mount table which mount
    covers the path *today* answers with the surviving parent once a nested
    mount drops, which is precisely the case where the path has become a bare
    directory that the real mount would later hide.
    """
    recipe = config.get("mount_recipe") or {}
    point = recipe.get("mountpoint") or ""
    return point if _mount_covers(point, config.get("upload_mount") or "") else ""


def backends_down(config: dict) -> list[str]:
    """Which of the worker's backing services are not answering.

    Postgres and Redis, by TCP connect. Deliberately not a query: a connect
    either completes or is refused, in milliseconds, and cannot be delayed by a
    filesystem. That distinction is the whole lesson of 1.11.1, where a check
    that could hang was used to decide whether to stop the worker.

    An ml-only node has neither configured and is never judged.
    """
    down = []
    for label, host_key, port_key, default in (
        ("Postgres", "db_hostname", "db_port", 5432),
        ("Redis", "redis_hostname", "redis_port", 6379),
    ):
        host = config.get(host_key)
        if not host:
            continue
        try:
            with socket.create_connection(
                (host, int(config.get(port_key, default))), timeout=2
            ):
                pass
        except (OSError, ValueError):
            down.append(label)
    return down


def library_mount_gone(config: dict) -> tuple[bool, str]:
    """Has the library's mount actually gone away? Returns (gone, mountpoint).

    Only the mount table is trusted for this. It is the one signal that means
    "the mount is not there" and nothing else, and reading it needs no child
    process, no timeout and no access to the mounted filesystem itself.

    1.11.0 asked the marker probe instead, which reads a file on the mount and
    is therefore also a permissions, timing and I/O question. On the release Mac
    that probe failed for ten minutes inside the running service while the same
    probe succeeded from a shell against the same healthy mount, and the worker
    was stopped for it. Stopping a working machine because a read failed is a
    much worse outcome than never noticing a mount had dropped.

    A library with no trusted mount is never judged: either it lives on a local
    disk, or it has not been seen healthy yet, and in both cases there is no
    absence to detect.
    """
    point = library_mount(config)
    if not point:
        return False, ""
    if is_mounted(point):
        return False, point
    return True, point


def media_io_healthy(config: dict) -> tuple[bool, str]:
    """Can we actually read the library? Advisory only, never a reason to stop
    the worker; the caller logs the reason so a failure is diagnosable rather
    than silent."""
    root = config.get("upload_mount") or ""
    media_id = config.get("media_id") or ""
    if not root or not media_id:
        return True, ""
    ok, _dangling, detail = _media_probe("verify", root, media_id, timeout=10)
    return ok, detail


def ensure_media_ready(config: dict) -> bool:
    """Confirm the media root is really present and writable before starting,
    establishing an id marker on first run. Returns True to proceed, False to
    refuse. Mount-agnostic; safe to leave on for local installs (the marker is
    created once and always present thereafter)."""
    media_root = config.get("upload_mount") or ""
    if not media_root:
        return True  # nothing configured to guard (same-host default path)

    def _report_dangling(paths: list[str]) -> None:
        """A media subdir points at a disk that isn't mounted (#115). Immich
        would fail every write to it, so name the exact path instead of letting
        thumbnail/transcode jobs die one by one with ENOENT."""
        log.error("Media location not ready: %s", media_root)
        for p in paths:
            log.error("  %s is a symlink to a target that does not exist.", p)
        log.error("  A media folder points at another disk (e.g. thumbs on an")
        log.error("  SSD) that isn't mounted. Mount it, or remove the symlink,")
        log.error("  then start again. Refusing to start so Immich can't fail")
        log.error("  every job that writes there.")

    media_id = config.get("media_id")
    if media_id:
        ok, dangling, detail = _media_probe("verify", media_root, media_id)
        if ok:
            return True
        if dangling:
            _report_dangling(dangling)
            return False
        # A probe that could not answer is not a probe that said no.
        #
        # This gate exists for one hazard: writing into a local placeholder
        # directory that the real mount would later hide. If the mount is in the
        # mount table, that hazard does not exist, whatever the probe managed to
        # read. And the probe can fail to answer for reasons that have nothing
        # to do with the library: on macOS a service reading a network volume
        # can block until the timeout, with no prompt anyone can answer, which
        # on the release Mac refused every worker start for weeks.
        covering = mount_recipe_for(media_root) or {}
        point = covering.get("mountpoint") or (config.get("mount_recipe") or {}).get(
            "mountpoint", ""
        )
        if point and is_mounted(point):
            log.warning(
                "Could not verify the library marker at %s (%s), but %s is "
                "mounted, so this is not a placeholder. Starting anyway.",
                media_root,
                detail or "no detail",
                point,
            )
            log.warning(
                "  If this keeps happening, give the accelerator Full Disk "
                "Access in System Settings > Privacy & Security: a background "
                "service reading a network volume can block without it."
            )
            return True

        log.error(
            "Media location not ready: %s (%s)", media_root, detail or "no detail"
        )
        log.error("  The marker identifying your media root is missing or")
        log.error("  unreadable. Usually a network mount isn't up yet, or the")
        log.error("  media path changed. Refusing to start so the worker can't")
        log.error("  write into a placeholder the mount would later mask.")
        log.error("  If you intentionally moved the media root, re-run setup.")
        return False

    # First run: establish the marker. Requires a present, writable root.
    new_id = uuid.uuid4().hex
    ok, dangling, _detail = _media_probe("init", media_root, new_id)
    if ok:
        config["media_id"] = new_id
        save_config(config)
        log.info("Media root verified and marked: %s", media_root)
        return True
    if dangling:
        _report_dangling(dangling)
        return False
    log.error("Media location not writable: %s", media_root)
    log.error("  Refusing to start — check the mount is up and writable.")
    return False


# Only our own variables can be set this way. Everything else a service needs
# is worked out here (ports, database settings, the model directory), and a
# config file that could quietly override those would be a way to break an
# install from a place nobody thinks to look.
_CONFIG_ENV_PREFIX = "IMMICH_ACCEL"


def config_env(config: dict | None = None) -> dict:
    """The IMMICH_ACCEL* variables set in config.json.

    They are documented, and until now there was no supported way to set them on
    the standard Homebrew install: brew services generates the plist, it carries
    no EnvironmentVariables, launchctl setenv does not reach the agent, and any
    hand edit is undone the next time the service restarts. The only way through
    was to wrap the binary in a script. config.json survives upgrades and
    restarts and is already where settings live. Reported by RxChi1d.
    """
    if config is None:
        # start_service runs before setup has written anything, and on a machine
        # with no config at all. No config simply means nothing set here.
        try:
            config = load_config()
        except (RuntimeError, OSError, ValueError):
            return {}
    raw = config.get("env")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        name = str(key)
        if name.startswith(_CONFIG_ENV_PREFIX):
            out[name] = str(value)
        else:
            log.warning(
                'Ignoring %s in the config "env" block: only %s* variables can '
                "be set there.",
                name,
                _CONFIG_ENV_PREFIX,
            )
    return out


# True only inside `immich-accelerator watch`. The watcher supervises the ML
# service it starts, so ML stays in the watcher's process group and ends with
# it. Every other entry point starts ML to outlive the command that asked for
# it, which is why `start` from a terminal keeps its own session. Set once, at
# the top of cmd_watch, and never cleared: a process runs one command.
_SUPERVISING_ML = False


def start_service(
    name: str, cmd: list[str], env: dict, cwd: str, *, own_session: bool = True
) -> int:
    """Start a background service and track its PID. Returns PID.

    own_session=True gives the service its own session, so it outlives the
    command that started it. `immich-accelerator start` needs that: measured on
    macOS 26.3, a service left in the caller's process group dies when the
    terminal that ran the command closes, while one in its own session does not.

    Pass own_session=False only for a service whose lifetime belongs to its
    supervisor. It then shares the supervisor's process group, which is what
    lets launchd's job cleanup, and a terminal hangup for a watcher run by
    hand, reach the service as well as the supervisor.
    """
    # Applied here rather than at each caller, so every service gets them and a
    # service added later does not have to remember.
    #
    # config first, so a real environment variable still wins. The file exists
    # because the environment cannot be reached on a Homebrew install, not to
    # outrank it: someone who exports one in a shell and runs the accelerator by
    # hand means it, and having a file quietly beat them is the opposite of what
    # everything else on a Unix box does.
    env = {**config_env(), **env}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{name}.log"
    cap_log(log_file)  # don't inherit a giant log across a (re)start
    fh = open(log_file, "a")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=own_session,
        )
    except Exception:
        fh.close()
        raise

    # Close fh immediately — Popen duplicated the fd
    fh.close()

    write_pid(name, proc.pid)
    if name == "worker":
        # Stamp the version this worker is running so watch can detect when a
        # later `brew upgrade` has left it on stale code.
        try:
            WORKER_VERSION_FILE.write_text(__version__)
        except OSError:
            pass

    # Check it's still alive after a moment
    time.sleep(2)
    if proc.poll() is not None:
        log.error("%s exited immediately. Check %s", name, log_file)
        lines = log_file.read_text().strip().split("\n")
        for line in lines[-10:]:
            log.error("  %s", line)
        if name == "worker":
            hint = diagnose_worker_log(log_file)
            if hint:
                for line in hint.split("\n"):
                    log.error("  %s", line)
        (PID_DIR / f"{name}.pid").unlink(missing_ok=True)
        raise RuntimeError(f"{name} failed to start")

    return proc.pid


def _pid_on_port(port: int) -> int | None:
    """PID listening on a TCP port (via lsof), or None."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return int(out.split()[0]) if out else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _dashboard_port() -> int:
    """Configured dashboard port (default 8420), so it can dodge a collision."""
    try:
        return int(load_config().get("dashboard_port", 8420))
    except (RuntimeError, ValueError, OSError):
        return 8420


# The three separable processes. Each can be turned off independently, and each
# is a real process boundary — which is exactly where this stops. Video,
# thumbnails and RAW decode all happen inside the single microservices worker,
# so "video but not thumbnails" is Immich's job scheduler, not ours.
COMPONENTS = ("worker", "ml", "dashboard")

# How to say each one in a sentence. str.capitalize() turns "ml" into "Ml".
COMPONENT_LABELS = {
    "worker": "Worker",
    "ml": "ML service",
    "dashboard": "Dashboard",
}


def _component_enabled(name: str, config: dict | None = None) -> bool:
    """Whether a component should be running. Defaults True.

    Absent means enabled, because every config written before these keys existed
    has none of them, and an upgrade must not silently turn anything off.

    An explicit component key beats the legacy "ml_only" preset. That order
    matters: without it, a user who ran `setup --ml-only` once could never turn
    the worker back on without hand-editing config.json.
    """
    if config is None:
        try:
            config = load_config()
        except (RuntimeError, ValueError, OSError):
            return True
    if name in config:
        return bool(config[name])
    if config.get("ml_only"):  # preset: worker off, everything else on
        return name != "worker"
    return True


def _dashboard_enabled() -> bool:
    """Whether the web dashboard is enabled. Set "dashboard": false in
    config.json (or `immich-accelerator component dashboard off`) to run
    headless."""
    return _component_enabled("dashboard")


def _process_is_our_dashboard(pid: int) -> bool:
    """Whether a pid is actually our dashboard, not a foreign port squatter.

    Guards the adopt-on-port path below: on some setups another process (e.g.
    OrbStack proxying a container) already listens on the dashboard port, and
    blindly adopting its pid meant we never started our own and pointed users
    at the wrong thing.
    """
    start = _get_process_start_time(pid)  # cheap "does it exist" + reuse guard
    if start is None:
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return "immich_accelerator" in out and "dashboard" in out


def stop_dashboard() -> bool:
    """Stop the dashboard, whether or not we still have a valid pidfile.

    The pidfile can go missing (an orphan from a prior run, or read_pid
    discarding it after PID reuse). start_dashboard has an adopt-the-orphan
    branch for exactly that state. Without the same fallback here, disabling the
    dashboard would leave it serving while every UI claims it is off. Returns
    whether something was stopped."""
    if read_pid("dashboard"):
        kill_pid("dashboard")
        return True
    orphan = _pid_on_port(_dashboard_port())
    if orphan and _process_is_our_dashboard(orphan):
        log.info("Stopping untracked dashboard (PID %d)", orphan)
        try:
            os.kill(orphan, signal.SIGTERM)
        except OSError as e:
            log.warning("  Could not stop PID %d: %s", orphan, e)
            return False
        return True
    return False


def reconcile_dashboard() -> bool:
    """Make the running state match the "dashboard" config key.

    One enforcement point, called from the watch loop as well as the toggle, so
    editing config.json (the documented way to turn it off) actually takes
    effect on a running install instead of only at the next start.

    Returns whether the running state now matches the config."""
    if _dashboard_enabled():
        return start_dashboard()
    if read_pid("dashboard") or _pid_on_port(_dashboard_port()):
        stop_dashboard()
    # "Off" means nothing of OURS is serving. Ask the port, not the pid file:
    # kill_pid unlinks the pid file unconditionally, so read_pid can never see a
    # survivor and this check would always report success.
    #
    # SIGTERM is asynchronous, so give it a moment before calling it a failure.
    # Reporting "it did not stop" for a process that is one scheduler tick from
    # exiting would be its own kind of lie.
    deadline = time.monotonic() + 3.0
    while True:
        survivor = _pid_on_port(_dashboard_port())
        if not (survivor and _process_is_our_dashboard(survivor)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def start_dashboard() -> bool:
    """Start the dashboard in the background if it isn't already running.
    Idempotent — used by both `start` and `watch` so either brings the
    dashboard up (#81). No-op when the dashboard is disabled in config.

    Returns whether the dashboard is (or was already) ours and running. False
    means we could not start it and said why, which is what lets a toggle report
    failure instead of claiming a success the user cannot see."""
    if not _dashboard_enabled():
        log.debug("Dashboard disabled in config; not starting.")
        return False
    if read_pid("dashboard"):
        return True  # already tracked + alive (also guards a just-spawned one)
    port = _dashboard_port()
    # Untracked but already serving (an orphan from a prior run whose pid file
    # was lost) — adopt its pid so a later stop can reach it. Only if it's
    # actually our dashboard, though: never adopt a foreign process that merely
    # happens to hold the port.
    orphan = _pid_on_port(port)
    if orphan:
        if _process_is_our_dashboard(orphan):
            write_pid("dashboard", orphan)
            log.info("Adopted running dashboard (PID %d)", orphan)
            return True
        log.warning(
            "Dashboard port %d is held by another process (PID %d), not the "
            'accelerator. Set "dashboard_port" in %s to a free port to run the '
            "dashboard.",
            port,
            orphan,
            CONFIG_FILE,
        )
        return False
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    dash_log = open(LOG_DIR / "dashboard.log", "a")
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                __package__ or "immich_accelerator",
                "dashboard",
                "--port",
                str(port),
            ],
            cwd=str(Path(__file__).parent.parent),
            stdout=dash_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        dash_log.close()
    write_pid("dashboard", proc.pid)
    log.info("Dashboard started: http://localhost:%d", port)
    return True


# --- Commands ---

_JF_FFMPEG_BASE = "https://repo.jellyfin.org/files/ffmpeg/macos/latest-7.x/arm64/"


def _find_jf_ffmpeg_url() -> str:
    """Find the latest jellyfin-ffmpeg download URL from the repo directory."""
    import urllib.request
    import html.parser

    class LinkParser(html.parser.HTMLParser):
        links: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                for name, val in attrs:
                    if name == "href" and val and val.endswith(".tar.xz"):
                        self.links.append(val)

    try:
        resp = urllib.request.urlopen(_JF_FFMPEG_BASE, timeout=10)
        parser = LinkParser()
        parser.links = []
        parser.feed(resp.read().decode())
        xz_files = [l for l in parser.links if "macarm64-gpl" in l]
        if xz_files:
            url = xz_files[-1]
            if not url.startswith("http"):
                url = _JF_FFMPEG_BASE + url
            return url
    except Exception:
        pass
    raise RuntimeError("Could not find jellyfin-ffmpeg download URL")


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

    url = _find_jf_ffmpeg_url()
    tar_path = jf_dir / "jellyfin-ffmpeg.tar.xz"
    try:
        urllib.request.urlretrieve(url, str(tar_path))
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

    # Install psql client for dashboard DB queries
    psql_path = "/opt/homebrew/opt/libpq/bin/psql"
    if not os.path.isfile(psql_path):
        _brew_install("libpq")

    return node, ffmpeg_path, ml_dir


def _validate_connectivity(config: dict) -> bool:
    """Check that DB and Redis are reachable. Returns True if all OK."""
    ok = True
    if not check_port(config["db_hostname"], int(config["db_port"]), "Postgres"):
        ok = False
    if not check_port(config["redis_hostname"], int(config["redis_port"]), "Redis"):
        ok = False
    return ok


# User choices that setup must not silently undo. Setup rebuilds config.json
# from scratch from what it detects, so anything the user set by hand or by
# toggle has to be carried across explicitly or it is lost on the next re-run.
#
# "ml" belongs here for the same reason "dashboard" does. Neither full setup
# path writes it, so leaving it out did not mean "setup decides", it meant
# "setup silently discards". Someone who offloaded ML to another machine
# (`component ml off` plus an "ml_url") and then re-ran setup, which is the
# documented repair step and what _require_worker_config's own error text
# tells them to do, would get a local engine started behind their back,
# several GB of models downloaded, and the worker repointed from the remote
# node back at localhost.
#
# "worker" is the one component key still not preserved, and that is
# deliberate: each setup path states whether it establishes a worker, so
# re-running a full `setup` on an ml-only box is how you turn the worker back
# on. `setup --ml-only` writes "worker": false itself.
# "env" belongs here for the same reason the rest do: setup rebuilds the config
# from scratch and saves it wholesale, so a key absent from this list is erased
# on every re-run. Leaving it out meant a user who chose software encoding for
# Docker-identical output silently went back to VideoToolbox the next time they
# ran the documented repair step.
_PRESERVED_CONFIG_KEYS = (
    "api_key", "ml_url", "dashboard_port", "dashboard", "ml", "env",
)


def _finalize_config(config: dict) -> None:
    """Carry over the user's own settings, save config, print next steps."""
    # Whether this machine had an install before this run, captured before
    # anything is written. Everything below rebuilds the config, so after
    # save_config there is no way to tell a first install from a repair.
    had_config_before = CONFIG_FILE.exists()
    try:
        existing = load_config()
        for key in _PRESERVED_CONFIG_KEYS:
            if key in existing and key not in config:
                config[key] = existing[key]
    except (RuntimeError, ValueError, OSError):
        pass

    if "api_key" not in config:
        log.info("")
        log.info(
            "Optional: add your Immich API key to enable the dashboard Re-queue button:"
        )
        log.info('  Edit %s and add: "api_key": "your-key-here"', CONFIG_FILE)
        log.info("  Generate a key in Immich → Administration → API Keys")

    # Only on a genuinely first install, meaning there was no config file at
    # all when setup started. An existing install keeps what it has, even if
    # that is "never chose", because it already has behaviour people depend on
    # and turning on the AudioToolbox encoder under them would change the bytes
    # of every video they process next.
    if not had_config_before:
        apply_encoding_preset("hardware", config)

    save_config(config)

    # Ensure /build firmlink for plugin path compatibility (Immich 2.7+).
    # Skip it when the worker is off: no worker, no Immich server files on this
    # box, so nothing needs /build to resolve.
    if _component_enabled("worker", config):
        _ensure_build_link()

    # Auto-start services
    log.info("")
    try:
        answer = input("  Start Immich Accelerator now? [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if not answer or answer == "y":
        cmd_start(argparse.Namespace(force=True))

    # Offer to install launchd service (watch mode — manages worker, ML, and dashboard).
    # Brew-installed users must use `brew services start immich-accelerator` instead:
    # the Homebrew formula defines its own service block, and brew services survives
    # upgrades correctly. A hand-rolled plist with a Cellar-versioned python path
    # would go stale the first time `brew upgrade` bumped the cellar.
    is_brew_install = "/Cellar/immich-accelerator/" in str(Path(__file__).resolve())
    plist_src = (
        Path(__file__).parent.parent / "launchd" / "com.immich.accelerator.plist"
    )
    plist_dst = (
        Path.home() / "Library" / "LaunchAgents" / "com.immich.accelerator.plist"
    )

    if is_brew_install:
        log.info("")
        log.info("Installed via Homebrew. To auto-start on login:")
        log.info("  brew services start immich-accelerator")
    elif plist_src.exists() and not plist_dst.exists():
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
            enabled = [c for c in COMPONENTS if _component_enabled(c, config)]
            log.info(
                "  Installed (auto-starts %s on login)",
                ", ".join(enabled) if enabled else "nothing, every component is off",
            )

    log.info("")
    log.info("Immich Accelerator is running.")


def _detect_docker_media_prefix(base_url: str, api_key: str) -> str | None:
    """Detect Docker's IMMICH_MEDIA_LOCATION via the Immich API.

    This is the path where the Docker side writes user uploads —
    NOT the path of any external library. Immich has two kinds of
    libraries:

      UPLOAD library   — implicit, rooted at IMMICH_MEDIA_LOCATION,
                         NOT returned by /api/libraries
      EXTERNAL library — user-defined folders with importPaths[],
                         returned by /api/libraries

    An earlier version of this probe used /api/libraries as the
    primary signal. That always returned an EXTERNAL library path
    (since upload libraries don't appear there), which is unrelated
    to upload_mount and produced false positives on any install
    with external libraries plus a correctly-configured upload root.

    The only reliable way to find the upload library's root is to
    parse an upload-library asset's originalPath. We filter for
    `libraryId: null` so external-library assets are skipped.

    Returns None if no upload-library assets exist yet (fresh
    install with external libs only) — caller treats None as
    "don't know, don't block".
    """
    import urllib.error
    import urllib.request

    if not api_key:
        return None

    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Ask /api/search/metadata for assets with no libraryId — those
    # are upload-library assets (user uploaded via web UI or API).
    # External-library assets always have a libraryId set, so this
    # filter cleanly separates the two cases.
    #
    # Note on size=5: we request 5 to give ourselves margin, but we
    # filter client-side — Immich doesn't accept a libraryId=null
    # filter in this endpoint. On pathological libraries where the
    # first 5 results happen to all be external-library assets,
    # we'll return None (silent "don't know"). That's a false
    # negative (no block when one might have been correct) rather
    # than a false positive, so it's safe — worst case the user
    # discovers a mismatch at first upload instead of at setup.
    try:
        body = json.dumps({"size": 5, "isNotInAlbum": False}).encode()
        req = urllib.request.Request(
            f"{base_url}/api/search/metadata",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return None

    items = []
    if isinstance(data, dict):
        items = data.get("assets", {}).get("items") or data.get("items") or []
    elif isinstance(data, list):
        items = data

    for asset in items:
        if not isinstance(asset, dict):
            continue
        # Skip external-library assets — we can't infer upload-root
        # from them, and they're what caused the false positive in
        # v1.4.1. libraryId is None/null/missing for upload assets.
        if asset.get("libraryId"):
            continue
        original = asset.get("originalPath")
        if not original:
            continue
        parts = Path(original).parts
        # Immich's own path builder (cores/storage.core.js):
        #   getLibraryFolder = join(mediaLocation, "library", storageLabel || ownerId)
        # so every uploaded original lives at:
        #   <IMMICH_MEDIA_LOCATION>/library/<storageLabel|ownerUUID>/<storage-template…>/<file>
        # The media root is therefore everything *before* the `library`
        # segment. This holds whether or not the user set a storage label —
        # the old UUID heuristic assumed `upload/<uuid>/` and silently
        # mis-detected a date-nested path for anyone with a storage label
        # (issue #61). Use the first `library` segment: a storage template
        # could itself contain a folder named "library", but the media root
        # would not normally.
        if "library" in parts:
            before = parts[: parts.index("library")]
            return str(Path(*before)) if before else None
        # Fallbacks for layouts without a `library` segment (very old installs
        # or non-standard configs). Try the legacy upload/<UUID> layout, then
        # a last-resort trim of the trailing <year>/<filename>.
        for i, p in enumerate(parts):
            if len(p) == 36 and p.count("-") == 4:
                before = parts[:i]
                if before and before[-1] == "upload":
                    before = before[:-1]
                return str(Path(*before)) if before else None
        if len(parts) >= 3:
            return str(Path(*parts[:-2]))
    return None


def _fetch_external_libraries(base_url: str, api_key: str) -> list[dict]:
    """Return the list of external-library dicts from /api/libraries.

    Immich's /api/libraries only includes EXTERNAL libraries — the
    upload library is implicit at IMMICH_MEDIA_LOCATION. Each entry
    has `name` and `importPaths`.
    """
    import urllib.error
    import urllib.request

    if not api_key:
        return []
    try:
        req = urllib.request.Request(
            f"{base_url}/api/libraries",
            headers={"x-api-key": api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list):
            return [lib for lib in data if isinstance(lib, dict)]
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        pass
    return []


def _is_top_level_path(path: str) -> bool:
    """True if `path` is a single top-level component under / (e.g. /data).

    synthetic.conf(5) can only create an entity whose name is a single
    component at the root mount point. A nested path like /usr/src/app/upload
    cannot be turned into a synthetic link (and /usr already exists), so the
    "mirror Docker's path on the Mac" route is only viable when Docker's media
    root is top-level (issue #61).
    """
    stripped = path.strip("/")
    return bool(stripped) and "/" not in stripped


def _warn_on_path_mismatch(immich_url: str, api_key: str, upload_mount: str) -> bool:
    """Validate that Docker-side paths resolve on this Mac.

    Two classes of check:
      (a) Upload library root — parsed from an upload asset's
          originalPath. Must match `upload_mount`. A real mismatch
          is FATAL and the caller should refuse to start — thumbnails
          will 404 for every web-UI upload.
      (b) External library importPaths — must exist on the local
          filesystem. Any missing path is a WARNING (not fatal):
          the worker can still process uploads and other libraries;
          it will just fail when it tries to touch the missing one.

    Returns True only for fatal (a) cases so the caller can block.
    Logs actionable guidance for both (a) and (b).
    """
    has_fatal = False

    # --- (a) upload root ---
    detected = _detect_docker_media_prefix(immich_url, api_key)
    if detected:
        detected_norm = detected.rstrip("/")
        mount_norm = upload_mount.rstrip("/")
        # upload_mount being a parent of detected is also fine
        # (e.g. upload_mount=/data matching detected /data/library).
        compatible = detected_norm == mount_norm or detected_norm.startswith(
            mount_norm + "/"
        )
        if not compatible:
            has_fatal = True
            log.error("")
            log.error("⚠  Upload path mismatch — thumbnails will 404")
            log.error("")
            log.error("   Docker Immich stores uploads under: %s", detected_norm)
            log.error("   Your upload_mount is set to:        %s", mount_norm)
            log.error("")
            if _is_top_level_path(detected_norm):
                # Docker's media root is a single top-level component, so the
                # Mac can reproduce it with a synthetic link. Offer both routes.
                log.error("   Two ways to fix this (see README 'Split deployment'):")
                log.error("")
                log.error("     A. Point Docker's IMMICH_MEDIA_LOCATION at your mount:")
                log.error("          %s", mount_norm)
                log.error("")
                log.error(
                    "     B. Make the Mac resolve Docker's path with a synthetic link:"
                )
                # printf, not echo: only zsh's echo expands \t — printf gives a
                # real tab in bash and zsh alike, so the synthetic entry is valid.
                log.error(
                    "          printf '%s\\t%s\\n' | sudo tee -a /etc/synthetic.d/immich-accelerator",
                    detected_norm.lstrip("/"),
                    mount_norm.lstrip("/"),
                )
                log.error(
                    "        Reboot, then re-run setup with upload_mount=%s",
                    detected_norm,
                )
            else:
                # Nested/system media root (e.g. the container default
                # /usr/src/app/upload). It can't be a synthetic link, so the
                # synthetic route needs a top-level media root first.
                log.error(
                    "   %s is nested — macOS synthetic links only create a single",
                    detected_norm,
                )
                log.error(
                    "   top-level name (e.g. /data), so it can't be mirrored as-is."
                )
                log.error(
                    "   Two ways forward (back up your DB first — paths migrate):"
                )
                log.error("")
                log.error("     Route 1 — point Docker at the path the Mac mounts:")
                log.error(
                    "          set IMMICH_MEDIA_LOCATION=%s, bind the volume there.",
                    mount_norm,
                )
                log.error("          No synthetic link needed; re-run setup as-is.")
                log.error("")
                log.error(
                    "     Route 2 — switch Docker to a top-level path, then synthesize:"
                )
                log.error(
                    "          set IMMICH_MEDIA_LOCATION=/data (bind the volume to /data),"
                )
                log.error(
                    "          printf 'data\\t%s\\n' | sudo tee -a /etc/synthetic.d/immich-accelerator",
                    mount_norm.lstrip("/"),
                )
                log.error("          Reboot, then re-run setup with upload_mount=/data")
            log.error("")

    # --- (b) external library paths ---
    missing_libs = []
    for lib in _fetch_external_libraries(immich_url, api_key):
        name = lib.get("name", "(unnamed)")
        for p in lib.get("importPaths", []) or []:
            if not isinstance(p, str) or not p:
                continue
            if not Path(p).exists():
                missing_libs.append((name, p))

    if missing_libs:
        log.warning("")
        log.warning(
            "⚠  External library paths not accessible on this Mac (%d):",
            len(missing_libs),
        )
        for name, p in missing_libs:
            log.warning("     %r → %s", name, p)
        log.warning("")
        log.warning(
            "   The worker will fail when processing assets from these libraries."
        )
        log.warning("   Mount each path on this Mac at the same absolute path, or add")
        log.warning("   a synthetic link so the Mac resolves it to your local mount.")
        log.warning("")

    return has_fatal


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


def _immich_clip_model(config: dict) -> str | None:
    """The CLIP model Immich is configured to send us, or None if unknown.

    Read from Immich's own system config so `ml-test` can say what the running
    setup actually uses instead of only the model it probes with (#116). Needs
    an api_key; any failure is non-fatal (the caller just stays quiet).
    """
    import urllib.error
    import urllib.request

    base, key = config.get("immich_url"), config.get("api_key")
    if not base or not key:
        return None
    req = urllib.request.Request(
        f"{base}/api/system-config", headers={"x-api-key": key}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    model = (data.get("machineLearning") or {}).get("clip", {}).get("modelName")
    return model if isinstance(model, str) and model else None


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
                # Stamp so the offline-imported build-data isn't seen as stale
                # by the cache gate later (which would force a re-download that
                # defeats the whole point of the offline import path).
                _finalize_build_data(build_data, bare_version)
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
    log.info("    ports: ['5432:5432']   # database")
    log.info("    ports: ['6379:6379']   # redis")
    log.info("")
    log.info("  (Use '127.0.0.1:5432:5432' to restrict to localhost if same machine)")
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


MANAGED_DOCKER_DIR = DATA_DIR / "docker"

# Template must track upstream Immich docker-compose.yml. Last synced
# with Immich v2.7.5 (2026-05). Key changes to watch: postgres image
# tag (vectorchord versions), valkey version, container internal paths.
_COMPOSE_TEMPLATE = """\
# Generated by immich-accelerator setup. Do not edit manually.
name: immich

services:
  immich-server:
    container_name: immich_server
    image: ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}
{user_line}    env_file: .env
    environment:
      - IMMICH_WORKERS_INCLUDE=api
      - IMMICH_MACHINE_LEARNING_URL=http://host.docker.internal:3003
      - IMMICH_MEDIA_LOCATION=${UPLOAD_LOCATION}
    volumes:
      - ${UPLOAD_LOCATION}:${UPLOAD_LOCATION}
      - {photos_mount}
      # Default data dir baked into the image; name it so it isn't an
      # anonymous volume orphaned on every down/up.
      - default_immich_datadir:/data
    ports:
      - '2283:2283'
    depends_on:
      - redis
      - database
    restart: unless-stopped

  database:
    container_name: immich_postgres
    image: ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0
    environment:
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_USER=postgres
      - POSTGRES_DB=immich
      - POSTGRES_INITDB_ARGS=--data-checksums
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - '127.0.0.1:5432:5432'
    restart: unless-stopped

  redis:
    container_name: immich_redis
    image: docker.io/valkey/valkey:9-alpine
    ports:
      - '127.0.0.1:6379:6379'
    restart: unless-stopped

volumes:
  pgdata:
  default_immich_datadir:
"""


def _find_docker_or_install() -> str:
    """Find a Docker runtime, or offer to install OrbStack."""
    candidates = [
        os.path.expanduser("~/.orbstack/bin/docker"),
        "/opt/homebrew/bin/docker",
        "/usr/local/bin/docker",
        "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # No Docker — offer OrbStack
    log.info("")
    log.info("No Docker runtime found.")
    try:
        answer = (
            input("  Install OrbStack (lightweight Docker for Mac)? [Y/n] ")
            .strip()
            .lower()
        )
    except EOFError:
        raise RuntimeError("Docker is required. Install OrbStack or Docker Desktop.")
    if answer and answer != "y":
        raise RuntimeError("Docker is required. Install OrbStack or Docker Desktop.")
    brew = _ensure_homebrew()
    if not brew:
        raise RuntimeError("Homebrew needed to install OrbStack.")
    log.info("  Installing OrbStack...")
    result = subprocess.run(
        [brew, "install", "--cask", "orbstack"],
        capture_output=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError("OrbStack install failed. Install manually: orbstack.dev")
    # OrbStack needs to be started
    log.info("  Starting OrbStack...")
    subprocess.run(["open", "-a", "OrbStack"], timeout=30)
    # Wait for Docker daemon
    docker = os.path.expanduser("~/.orbstack/bin/docker")
    for _ in range(30):
        if _docker_is_running(docker):
            log.info("  OrbStack ready")
            return docker
        time.sleep(2)
    raise RuntimeError(
        "OrbStack installed but Docker daemon didn't start. Try: open -a OrbStack"
    )


def _ensure_docker_running(docker: str) -> None:
    """Make sure the Docker daemon is up."""
    if _docker_is_running(docker):
        return
    # Try starting it
    log.info("Docker not running, starting...")
    if "orbstack" in docker.lower() or os.path.exists("/Applications/OrbStack.app"):
        subprocess.run(["open", "-a", "OrbStack"], timeout=10)
    else:
        subprocess.run(["open", "-a", "Docker"], timeout=10)
    for _ in range(15):
        if _docker_is_running(docker):
            return
        time.sleep(2)
    raise RuntimeError("Could not start Docker. Start it manually and re-run setup.")


def _fresh_install(docker: str) -> bool:
    """Set up Immich from scratch. Returns True if successful."""
    log.info("")
    log.info("No Immich instance found. Set up a fresh one?")
    try:
        answer = input("  [Y/n] ").strip().lower()
    except EOFError:
        return False
    if answer and answer != "y":
        return False

    # Ask for paths
    log.info("")
    default_photos = str(Path.home() / "Pictures")
    try:
        photos_path = input(
            f"  Where are your photos stored? [{default_photos}]: "
        ).strip()
    except EOFError:
        photos_path = ""
    photos_path = photos_path or default_photos
    if not Path(photos_path).is_dir():
        log.error("Directory does not exist: %s", photos_path)
        return False

    default_data = str(DATA_DIR / "data")
    try:
        data_path = input(
            f"  Where should Immich store its data? [{default_data}]: "
        ).strip()
    except EOFError:
        data_path = ""
    data_path = data_path or default_data
    Path(data_path).mkdir(parents=True, exist_ok=True)

    # Run the server container as the invoking user instead of root.
    # The immich-server container only needs to read your photos (mounted
    # read-only) and read/write the media/data directory. Running it as
    # your own UID means everything it writes to those bind mounts is
    # owned by you, not root — so the data dir stays removable without
    # sudo and there are no permission surprises later. Postgres and Redis
    # are left as their default (root) users: they only touch a named
    # volume and the network, never your host files, so there's nothing to
    # gain by changing them. No GID is needed — Docker defaults the
    # supplementary group to 0, which is fine for owner-writable mounts.
    run_as_user = True
    log.info("")
    try:
        answer = (
            input(
                "  Run the Immich server container as the current user "
                f"(uid {os.getuid()})? [Y/n] "
            )
            .strip()
            .lower()
        )
    except EOFError:
        answer = ""
    if answer and answer != "y":
        run_as_user = False
    user_line = f'    user: "{os.getuid()}"\n' if run_as_user else ""

    # Check ports
    for port, label in [(2283, "Immich"), (5432, "Postgres"), (6379, "Redis")]:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                log.error("Port %d (%s) is already in use.", port, label)
                return False
        except OSError:
            pass  # Good — port is free

    # Generate compose + env
    compose_dir = MANAGED_DOCKER_DIR
    compose_dir.mkdir(parents=True, exist_ok=True)

    # Photos mount: same absolute path inside container, read-only.
    # Use str.replace instead of str.format to avoid issues with
    # curly braces in paths or the Docker ${{}} env var syntax.
    photos_mount = f"{photos_path}:{photos_path}:ro"
    compose_content = _COMPOSE_TEMPLATE.replace("{photos_mount}", photos_mount).replace(
        "{user_line}", user_line
    )

    (compose_dir / "docker-compose.yml").write_text(compose_content)

    import secrets

    db_password = secrets.token_urlsafe(24)
    # All vars the Immich server reads from .env (via env_file).
    # Must match what stock Immich docker-compose expects.
    env_content = (
        f"UPLOAD_LOCATION={data_path}\n"
        f"DB_PASSWORD={db_password}\n"
        f"DB_HOSTNAME=immich_postgres\n"
        f"DB_USERNAME=postgres\n"
        f"DB_DATABASE_NAME=immich\n"
        f"REDIS_HOSTNAME=immich_redis\n"
    )
    (compose_dir / ".env").write_text(env_content)
    os.chmod(compose_dir / ".env", 0o600)

    log.info("")
    log.info("Creating Immich Docker stack...")
    result = subprocess.run(
        [docker, "compose", "-f", str(compose_dir / "docker-compose.yml"), "up", "-d"],
        capture_output=False,
        timeout=300,
    )
    if result.returncode != 0:
        log.error("Docker compose failed. Check the output above.")
        return False

    # Wait for API
    log.info("Waiting for Immich to start...")
    for i in range(60):
        try:
            import urllib.request

            with urllib.request.urlopen(
                "http://localhost:2283/api/server/ping", timeout=2
            ) as r:
                if b"pong" in r.read():
                    log.info("  Immich server ready")
                    break
        except Exception:
            pass
        time.sleep(3)
    else:
        log.error("Immich did not start within 3 minutes.")
        return False

    return True


def _setup_local(args):
    """Setup from local Docker (original behavior, with fresh install support)."""
    log.info("Detecting Immich instance...")

    # Step 1: Find or install Docker
    try:
        docker = _find_docker_or_install()
    except RuntimeError as e:
        log.error("%s", e)
        return
    _ensure_docker_running(docker)

    # Step 2: Check for existing Immich
    try:
        immich = detect_immich(docker)
    except RuntimeError:
        # No running Immich — check if we have a managed compose
        managed_compose = MANAGED_DOCKER_DIR / "docker-compose.yml"
        if managed_compose.exists():
            log.info("Found managed Immich stack — starting it...")
            subprocess.run(
                [docker, "compose", "-f", str(managed_compose), "up", "-d"],
                capture_output=False,
                timeout=300,
            )
            # Wait briefly for containers
            for _ in range(30):
                try:
                    immich = detect_immich(docker)
                    break
                except RuntimeError:
                    time.sleep(2)
            else:
                log.error("Managed stack did not start. Check: docker compose logs")
                return
        else:
            # No Immich at all — offer fresh install
            if not _fresh_install(docker):
                return
            try:
                immich = detect_immich(docker)
            except RuntimeError:
                log.error("Fresh install completed but could not detect Immich.")
                return

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
    # Prefer upload_mount (from Docker volume inspection), fall back to
    # media_location (from IMMICH_MEDIA_LOCATION env). Both point to the
    # same directory when the compose uses same-path mounts.
    upload = immich["upload_mount"] or immich.get("media_location")

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
        # Use 127.0.0.1, not "localhost": on macOS localhost resolves to
        # ::1 (IPv6) first, but the Docker/OrbStack stack publishes these
        # ports on 127.0.0.1 (IPv4) only, so "localhost" can hit ::1 and
        # fail or stall. 127.0.0.1 connects directly to the published port.
        "db_hostname": "127.0.0.1",
        "db_port": immich["db_port"],
        "db_username": immich["db_username"],
        "db_password": immich["db_password"],
        "db_name": immich["db_name"],
        "redis_hostname": "127.0.0.1",
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
    redis_username = prompt("Redis username [blank if none]", "")
    redis_password = getpass.getpass("  Redis password [blank if none]: ").strip()

    # Probe Docker's view of the media root so we can surface mismatch
    # up-front rather than when thumbnails 404 (issue #19). Requires
    # API key — prompt for one if the user didn't pass it.
    if not api_key:
        log.info("")
        log.info("Your Immich API key (Settings → API Keys in the web UI) lets us")
        log.info(
            "detect Docker's media path and prevent thumbnail 404s in split setups."
        )
        log.info("Leave blank to skip the check.")
        api_key = getpass.getpass("  Immich API key (optional): ").strip()

    detected_prefix = _detect_docker_media_prefix(url, api_key) if api_key else None
    if detected_prefix:
        log.info("")
        log.info("Docker Immich is using this as its media root: %s", detected_prefix)
        log.info("Your upload_mount MUST produce that same absolute path on this Mac.")
        log.info("(See README → Split deployment for the two standard topologies.)")
        log.info("")
        default_mount = detected_prefix
    else:
        default_mount = ""

    upload_mount = prompt("Upload/media path (as mounted on this Mac)", default_mount)

    if api_key and upload_mount:
        if _warn_on_path_mismatch(url, api_key, upload_mount):
            # Real mismatch detected. Offer to abort so the user can
            # fix the topology before we save a broken config.
            try:
                answer = (
                    input("  Save config anyway and fix later? [y/N] ").strip().lower()
                )
            except EOFError:
                answer = "n"
            if answer != "y":
                log.info("Aborted. Re-run setup after fixing the path mapping.")
                return

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
        # The API already reported the exact version, so the cache can be settled
        # before deciding how to fetch. Both fetch paths make this same check, but
        # extract_immich_server only reaches it after `docker pull` has run.
        bare_version = version.lstrip("v")
        server_dir = _cached_server_if_current(
            DATA_DIR / "server" / bare_version, bare_version
        )

    if not args.import_server and server_dir is None:
        # Try local Docker pull
        try:
            docker = _find_running_docker()
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
        except (
            RuntimeError,
            subprocess.SubprocessError,
            FileNotFoundError,
            OSError,
        ) as err:
            log.info("  Local Docker extraction failed (%s)", err)
            log.info("  Downloading server from ghcr.io instead...")
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
        "redis_username": redis_username,
        "redis_password": redis_password,
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
        # node@22 is the LTS we pin for sharp ABI compat. `start`
        # re-resolves via find_node() on every run, so setting this
        # to the wrong path is self-healing — but picking the right
        # default keeps the manual config honest.
        "node": "/opt/homebrew/opt/node@22/bin/node",
        "immich_url": "http://YOUR_IMMICH_HOST:2283",
        "db_hostname": "YOUR_DB_HOST",
        "db_port": "5432",
        "db_username": "postgres",
        "db_password": "YOUR_DB_PASSWORD",
        "db_name": "immich",
        "redis_hostname": "YOUR_REDIS_HOST",
        "redis_port": "6379",
        "redis_username": "",
        "redis_password": "",
        "upload_mount": "/path/to/immich/upload",
        "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
        "ml_dir": str(Path(__file__).parent.parent / "ml"),
        "ml_port": 3003,
        "api_key": "YOUR_API_KEY (optional, for dashboard re-queue)",
    }

    # A template is a first install by definition, so it starts at an end
    # rather than in the middle. Without this, the manual path never reaches
    # _finalize_config, the config exists by the time any later setup run
    # looks, and a brand new install opens on Custom.
    apply_encoding_preset("hardware", template)

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


def _setup_ml_only(args) -> None:
    """Set up this Mac as an ML-only network compute node: just the ML engine
    (native Swift by default, Python venv fallback), reachable at this Mac's
    IP on ml_port. No Docker, no Postgres, no Redis, no worker, no library
    mount. Point another Immich instance's Administration -> Machine Learning
    Settings -> Remote Machine Learning URL at this Mac.
    """
    log.info("Setting up ML-only mode (network ML compute node)...")
    log.info("  This Mac will run only the ML engine — no worker, no Docker, no DB.")

    ml_dir = _find_ml_dir()
    if ml_dir:
        log.info("  Python venv fallback: %s", ml_dir)
    else:
        log.warning(
            "  Python ML source/venv not found — the venv fallback engine "
            "won't be available."
        )
        log.warning(
            "  The native Swift engine (if installed via Homebrew) still "
            "works on its own."
        )

    # "worker": false is the real switch; "ml_only" is kept alongside it as the
    # documented preset name so the flag and the config key still line up for
    # anyone reading either one.
    config = {
        "ml_only": True,
        "worker": False,
        "ml": True,
        "ml_dir": str(ml_dir) if ml_dir else None,
        "ml_port": 3003,
    }
    _finalize_config(config)

    log.info("")
    log.info("Point other Immich instances' Remote Machine Learning URL at:")
    log.info("  http://<this-mac-ip>:%d", config["ml_port"])


def cmd_setup(args):
    """Set up the accelerator. Dispatches to local, remote, manual, or ml-only mode."""
    if args.ml_only:
        _setup_ml_only(args)
    elif args.manual:
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


# Version-independent native model asset (CLIP safetensors + tokenizer + ArcFace).
# Fetched once into ~/.cache/immich-ml-native and reused across upgrades.
NATIVE_MODEL_URL = (
    "https://github.com/epheterson/immich-apple-silicon/releases/download/"
    "clip-model-v1/native-models.tar.gz"
)
NATIVE_CACHE = Path.home() / ".cache" / "immich-ml-native"


def _native_bundle_dir(config: dict) -> Path | None:
    """The installed native bundle dir (with the immich-ml-native binary), or None."""
    candidates = []
    if config.get("native_ml_dir"):
        candidates.append(Path(config["native_ml_dir"]))
    candidates.append(Path("/opt/homebrew/opt/immich-accelerator/libexec/native-ml"))
    candidates.append(Path.home() / ".immich-accelerator" / "native-ml")
    return next((b for b in candidates if (b / "immich-ml-native").exists()), None)


def _native_clip_dir(config: dict) -> Path:
    return Path(config.get("native_clip_dir") or (NATIVE_CACHE / "clip"))


def _native_arcface(config: dict) -> Path | None:
    """Resolve the ArcFace ONNX: explicit config, the InsightFace cache (existing
    users already have buffalo_l), or the native model cache (fresh fetch)."""
    if config.get("native_arcface"):
        p = Path(config["native_arcface"])
        return p if p.exists() else None
    for p in (
        Path.home() / ".insightface" / "models" / "buffalo_l" / "w600k_r50.onnx",
        NATIVE_CACHE / "arcface" / "w600k_r50.onnx",
    ):
        if p.exists():
            return p
    return None


def _native_ml_spec(config: dict, env: dict):
    """Launch spec (cmd, cwd, env) for the native Swift ML engine, or None.

    Native is the default. It needs the relocatable bundle (immich-ml-native +
    colocated mlx.metallib + libonnxruntime), the CLIP safetensors, and the
    ArcFace ONNX. Requires ALL of them before choosing native, so a fresh install
    where a model is not fetched yet falls back cleanly to the venv instead of
    starting native with broken CLIP or face recognition.
    """
    bundle = _native_bundle_dir(config)
    if bundle is None:
        return None
    clip_dir = _native_clip_dir(config)
    arcface = _native_arcface(config)
    if not (clip_dir / "model.safetensors").exists() or arcface is None:
        return None
    env = dict(env)
    env["ML_CLIP_DIR"] = str(clip_dir)
    env["ML_ARCFACE"] = str(arcface)
    ml_port = int(config.get("ml_port", 3003))
    return [str(bundle / "immich-ml-native"), "serve", str(ml_port)], str(bundle), env


def _maybe_fetch_native_models(config: dict) -> None:
    """If the native bundle is installed but its models are missing, kick off a
    one-time background download (~740 MB: CLIP + ArcFace) into ~/.cache and
    return. The accelerator uses the venv until the models arrive; a later ML
    (re)start then picks up native. Keeps installs fast and upgrades cheap (the
    models persist across versions). Guarded so it is not started twice.
    """
    if config.get("ml_engine") == "python" or _native_bundle_dir(config) is None:
        return
    have_clip = (_native_clip_dir(config) / "model.safetensors").exists()
    have_face = _native_arcface(config) is not None
    if have_clip and have_face:
        return
    marker = NATIVE_CACHE / ".fetching"
    if marker.exists():
        try:
            if time.time() - float(marker.read_text().strip()) < 3600:
                return  # a fetch is already in flight
        except (ValueError, OSError):
            pass
    url = config.get("native_model_url") or NATIVE_MODEL_URL
    try:
        NATIVE_CACHE.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()))
    except OSError:
        return
    tarball = NATIVE_CACHE / "native-models.tar.gz"
    script = (
        f'curl -fL --retry 3 -o "{tarball}" "{url}" && '
        f'tar -xzf "{tarball}" -C "{NATIVE_CACHE}" && rm -f "{tarball}"; '
        f'rm -f "{marker}"'
    )
    try:
        logf = open(LOG_DIR / "native-model-fetch.log", "a")
        subprocess.Popen(
            ["bash", "-c", script],
            start_new_session=True,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
        log.info(
            "Native ML models missing; fetching in the background (~740MB one time). "
            "Using the Python engine until they arrive."
        )
    except OSError:
        marker.unlink(missing_ok=True)


def _venv_ml_spec(config: dict, env: dict):
    """Launch spec (cmd, cwd, env) for the Python venv ML service, or None."""
    ml_dir = Path(config.get("ml_dir", ""))
    ml_python = ml_dir / "venv" / "bin" / "python3"
    if ml_python.exists():
        # stdout is block-buffered once it's not a tty (always true — we redirect
        # it into ml.log), which is where uvicorn's access log and any print()
        # output live. Without this, `logs ml` sits blank and then dumps a stale
        # chunk instead of streaming in real time.
        # WEB_CONCURRENCY is inherited from whatever launched the supervisor,
        # and uvicorn's Config reads it when workers is None, which is how
        # src.main calls uvicorn.run. A value above one forks that many copies of
        # the ML service, each loading its own multi-gigabyte model, on a Mac
        # chosen for this work precisely because memory is the scarce resource.
        # It also leaves several processes on the port under a master that
        # ml.pid does not name, and nothing here is written to expect that.
        env = dict(env, PYTHONUNBUFFERED="1", WEB_CONCURRENCY="1")
        return [str(ml_python), "-m", "src.main"], str(ml_dir), env
    return None


def _ml_ping(config: dict, timeout: float = 3.0) -> bool:
    """One /ping probe. True only if the service answered."""
    import urllib.request

    port = int(config.get("ml_port", 3003))
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/ping", timeout=timeout
        ) as r:
            return r.read().strip() == b"pong"
    except Exception:
        return False


def _ml_healthy(config: dict, pid: int | None = None, timeout: float = 90.0) -> bool:
    """Poll the ML service /ping until it answers or the timeout elapses.

    The native engine's first start is slow: it loads ~700 MB of weights and
    compiles mlx's Metal pipeline on first use (a cold start measured well over
    25 s; warm starts are a few seconds). So the window is generous. But if
    ``pid`` is given and the process dies, bail immediately so a genuinely
    broken native falls back to the venv fast instead of waiting out the timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid is not None:
            try:
                os.kill(pid, 0)
            except OSError:
                return False  # process died, fall back now
        if _ml_ping(config):
            return True
        time.sleep(1)
    return False


def _start_ml_preferred(config: dict):
    """Start the preferred ML engine (native by default) WITHOUT waiting for it
    to become healthy. Returns (pid, engine_label, is_native) or (None, None, False).

    Native is the default (set ``ml_engine: python`` to force the venv). If native
    can't even start (missing bundle, or it crashes within start_service's liveness
    check), we fall back to the venv immediately. The slow "started but not yet
    healthy" case is handled separately by _ml_verify_or_fallback, so a native cold
    start never blocks the caller (e.g. the worker startup).
    """
    env = os.environ.copy()
    attempts = []
    if config.get("ml_engine", "native") != "python":
        native = _native_ml_spec(config, env)
        if native is not None:
            attempts.append(("native Swift", native, True))
        else:
            # Native bundle may be installed but its models not fetched yet;
            # kick off the one-time background download and use the venv for now.
            _maybe_fetch_native_models(config)
    venv = _venv_ml_spec(config, env)
    if venv is not None:
        attempts.append(("Python venv", venv, False))

    if not attempts:
        # Said here rather than by the callers: they see only "no pid", which is
        # also what a refused start returns, and cannot tell the two apart.
        log.warning("ML service will not start (no native bundle, no venv).")
        log.warning(
            "  If you installed via Homebrew, try: brew reinstall immich-accelerator"
        )
        return None, None, False

    port = int(config.get("ml_port", 3003))
    for label, (cmd, cwd, senv), is_native in attempts:
        # Re-checked before every attempt rather than once for the batch. The
        # native engine exits on a bind conflict, and the fallback below starts
        # the venv on any RuntimeError without knowing which failure it was, so
        # the second engine needs its own answer about the port.
        state = ml_port_state(port)
        if state != PORT_FREE:
            log.warning(
                "Not starting the %s ML engine: port %d is %s.",
                label,
                port,
                (
                    "already in use"
                    if state == PORT_OCCUPIED
                    else "in an unknown state (could not inspect it)"
                ),
            )
            return None, None, False
        log.info("Starting ML service (%s)...", label)
        try:
            pid = start_service("ml", cmd, senv, cwd, own_session=not _SUPERVISING_ML)
            return pid, label, is_native
        except RuntimeError:
            log.warning("  %s failed to start", label)
            continue
    return None, None, False


def port_in_use(port: int) -> bool:
    """Is anything listening on this port right now?

    A plain TCP connect, deliberately not /ping: the case this exists for is a
    wedged service that still holds the socket but answers nothing, which is
    exactly what a health check cannot see.

    Both stacks are tried. The native engine binds IPv6, and while macOS maps
    an IPv4 loopback connect onto a dual-stack listener, a listener that is
    IPv6-only would be invisible to an AF_INET connect alone, and "nothing is
    listening" is the answer that lets a second engine start on top of it.
    """
    # create_connection rather than connect_ex: on a socket with a timeout,
    # connect_ex can report EINPROGRESS instead of success, which reads as
    # "nothing is listening" and lets a second engine start on top of a live
    # one. create_connection either returns a connected socket or raises, and
    # it is what check_port has always used here.
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except (OSError, ValueError):
            continue
    return False


def wait_for_port_free(port: int, timeout: float = 10.0) -> bool:
    """Wait for a killed service to actually let go of its port.

    Process exit and socket release are not the same instant, and the native
    engine now exits rather than lingering when it cannot bind (#38). Starting
    the replacement too early therefore means the native start fails, the
    caller falls back to the Python venv, and the machine quietly runs on the
    slow engine until somebody restarts it by hand.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_in_use(port):
            return True
        time.sleep(0.25)
    return not port_in_use(port)


def _ml_verify_or_fallback(config: dict, pid: int, engine: str):
    """Verify the (already-started) native engine becomes healthy; if not, kill
    it and start the Python venv. No-op for the venv engine. Returns (pid, engine).
    """
    if _ml_healthy(config, pid=pid):
        return pid, engine
    log.warning("  native ML did not become healthy; falling back to venv")
    kill_pid("ml")
    if not wait_for_port_free(config.get("ml_port", 3003)):
        # The return value used to be discarded and the venv started anyway,
        # into a port the engine we just killed had not let go of. Do not start
        # a second engine while the port is still occupied; the next cycle can
        # try again, for as many cycles as the port stays held.
        log.warning(
            "  port %s is still held; leaving the restart for the next cycle "
            "rather than starting a second engine into a bind conflict.",
            config.get("ml_port", 3003),
        )
        return None, None
    venv = _venv_ml_spec(config, os.environ.copy())
    if venv is not None:
        cmd, cwd, senv = venv
        log.info("Starting ML service (Python venv)...")
        try:
            return (
                start_service("ml", cmd, senv, cwd, own_session=not _SUPERVISING_ML),
                "Python venv",
            )
        except RuntimeError:
            log.warning("  Python venv failed to start")
    return None, None


def _start_ml_service(config: dict):
    """Start the ML service (native preferred, venv fallback) and verify it.
    Blocks on the native health check, so use in the watch loop where there is no
    worker to hold up; cmd_start uses the split form to avoid delaying the worker.
    """
    pid, engine, is_native = _start_ml_preferred(config)
    if pid and is_native and engine:
        return _ml_verify_or_fallback(config, pid, engine)
    return pid, engine


# Our two ML engines, as they appear in `ps`. Anything else listening on the
# port belongs to somebody else (usually Docker's immich-machine-learning).
_ML_CMD_RE = re.compile(r"immich-ml-native\s+serve|python[^\s]*\s+-m\s+src\.main")


def _our_ml_process(port: int) -> int | None:
    """PID of an ML engine of ours that is running, found by process table.

    The fallback for when lsof cannot answer. It walks every descriptor on the
    machine, so one unresponsive network mount stalls it past any timeout, and
    the release Mac is exactly that machine. ps does not touch the filesystem.

    This says "an engine of ours is running", not "it owns the port", so the
    caller must have established that something holds the port first. Together
    those are the same conclusion, since our engine binds that port or exits.
    """
    try:
        out = subprocess.run(
            ["/bin/ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # The port matters. The native engine takes it in argv, so require it:
    # otherwise any engine of ours anywhere is treated as the holder of this
    # port. The preflight gate runs one on another port on this same Mac, and
    # Docker's own ML container is a real holder of 3003, so the combination
    # adopted the wrong process and would later have signalled it.
    native = re.compile(rf"immich-ml-native\s+serve\s+{int(port)}(\s|$)")
    venv = re.compile(r"python[^\s]*\s+-m\s+src\.main")
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit():
            continue
        if native.search(cmd):
            return int(pid)
        # The venv engine gets its port from ML_PORT, not argv, so its command
        # line cannot tell this port from another. Without checking, the
        # preflight gate's own engine on a different port was a match, which is
        # the case requiring the port was added for.
        if venv.search(cmd) and _venv_ml_port(int(pid)) == int(port):
            return int(pid)
    return None


def _venv_ml_port(pid: int) -> int | None:
    """The port a running venv engine was given, read from its environment."""
    try:
        env = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-Eww", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hit = re.search(r"\bML_PORT=(\d+)", env)
    return int(hit.group(1)) if hit else 3003


def _listener_pid(port: int) -> int | None:
    """PID of whatever is listening on this port, or None.

    lsof is the precise answer and the one that can identify a process that is
    not ours, so it is tried first, but it cannot be relied on: see
    _our_ml_process for why it stalls on the release Mac.
    """
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return _our_ml_process(port) if port_in_use(port) else None
    first = out.splitlines()[0].strip() if out else ""
    if first.isdigit():
        return int(first)
    return _our_ml_process(port) if port_in_use(port) else None


# Three answers, not two. "Cannot tell" blocks a start as firmly as "occupied":
# what a start must never do is race a process that already holds the port, and
# an inspection that failed says nothing about whether one does. Reporting a
# failed inspection as free would put back exactly the behaviour this replaces.
PORT_FREE = "free"
PORT_OCCUPIED = "occupied"
PORT_UNKNOWN = "unknown"


def ml_port_state(port: int) -> str:
    """Whether anything holds this port, or whether we could not find out.

    lsof exits 0 with the pid when something holds the port, and 1 when nothing
    matches. It also exits 1 on its own errors, so "free" needs a clean stderr
    as well. Anything else — lsof is missing, it hung, it answered in a shape we
    do not recognise — is unknown, never "free".
    """
    # Ask the network stack first. A connect either succeeds or is refused, in
    # microseconds, and it cannot be delayed by anything on disk.
    #
    # lsof can: it walks every open descriptor on the machine, so a single
    # unresponsive network mount stalls it until the timeout. On the release
    # Mac, with the library on NFS, connect answers in 0.000s while lsof times
    # out after 10. Deciding this question with lsof there meant the port was
    # permanently "unknown", and an engine that is never allowed to start is a
    # worse failure than the double-start this guard exists to prevent.
    if port_in_use(port):
        return PORT_OCCUPIED

    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        # connect said nothing is listening. lsof was only going to confirm it.
        return PORT_FREE
    out = result.stdout.strip()
    if (
        result.returncode == 0
        and out.splitlines()
        and out.splitlines()[0].strip().isdigit()
    ):
        return PORT_OCCUPIED
    # Exit 1 is also how lsof reports its own errors, so an empty stdout alone
    # does not mean "nothing matched". -t selects -w (lsof(8)), which suppresses
    # the warnings lsof prints for unreadable mounts, so anything left on stderr
    # is a real error and must not read as free.
    if result.returncode == 1 and not out and not result.stderr.strip():
        return PORT_FREE
    return PORT_UNKNOWN


def adopt_live_ml(config: dict) -> int | None:
    """Take back an ML service that is ours but has lost its pidfile.

    A pidfile can go missing while the process it named is perfectly healthy: a
    failed restart that clears it, a crash between spawn and write, a stray
    `stop`. Before this, the watcher pinged the port, got an answer from a
    process it no longer had a PID for, and classified its own engine as a
    foreign listener to be left alone. It then stayed left alone: status and
    the menu bar reported ML stopped while it was serving, and nothing would
    have restarted it if it wedged. Found on the release Mac, four days after
    it happened.
    """
    port = config.get("ml_port", 3003)
    if not port_in_use(port):
        return None
    # Ours first, from the process table. That answers the question this
    # function actually asks, costs nothing, and skips lsof entirely in the
    # common case; lsof is only needed to recognise a holder that is not ours,
    # and it can take ten seconds to say so on a Mac with a network mount.
    pid = _our_ml_process(int(port)) or _listener_pid(port)
    if pid is None:
        return None
    try:
        cmd = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not _ML_CMD_RE.search(cmd):
        return None  # somebody else's service; the caller leaves it alone
    write_pid("ml", pid)
    log.info("Adopted the running ML service (PID %d); its pidfile was missing.", pid)
    return pid


def reconcile_ml(config: dict) -> None:
    """Make the running state match the "ml" config key, the ML counterpart to
    reconcile_dashboard. Re-resolves ml_dir first, because brew upgrade deletes
    the old Cellar path out from under the cached value (#29)."""
    global _ml_unresponsive_since, _ml_foreign_listener_warned

    if not _component_enabled("ml", config):
        if read_pid("ml"):
            log.info("ML disabled in config, stopping it.")
            kill_pid("ml")
        _ml_unresponsive_since = None
        return
    pid = read_pid("ml")
    wedged = False
    if not pid:
        # No process means no silence to hold against one. Leaving the timer
        # set here was a restart loop waiting to happen: a service that went
        # quiet and then died on its own left the clock running, so the
        # replacement was judged from the dead instance's start and got killed
        # on its first quiet tick, which for a cold start is every time.
        _ml_unresponsive_since = None
    if pid:
        # A live PID is not a working service. The old check ended here, so a
        # process that was up but answering nothing kept its place forever and
        # nothing ever restarted it: that is how a Mac ran for days on an ML
        # service the accelerator believed it was managing but wasn't.
        if _ml_ping(config):
            _ml_unresponsive_since = None
            return
        now = time.monotonic()
        # A different PID is a different service, and it has been quiet for no
        # time at all. Without this the timer is just a wall clock: a process
        # replaced between two ticks (a manual `restart`, or a crash and
        # relaunch inside one interval) is judged from its predecessor's
        # silence and killed mid cold-start, then so is its replacement.
        if _ml_unresponsive_since is None or _ml_unresponsive_since[0] != pid:
            _ml_unresponsive_since = (pid, now)
            return
        stuck = now - _ml_unresponsive_since[1]
        # Silence alone isn't enough to act on. A cold native start loads
        # weights before it binds, and a first-use model fetch runs for
        # minutes; restarting into either would kill the very work being
        # waited on. Only sustained silence counts.
        if stuck < ML_UNRESPONSIVE_GRACE:
            return
        log.warning(
            "ML process %d has not answered for %ds, restarting it.", pid, int(stuck)
        )
        kill_pid("ml")
        if not wait_for_port_free(config.get("ml_port", 3003)):
            log.warning(
                "  Port %s is still held after stopping the wedged ML service; "
                "leaving it for the next cycle rather than starting a second "
                "one into a bind conflict.",
                config.get("ml_port", 3003),
            )
            _ml_unresponsive_since = None
            return
        _ml_unresponsive_since = None
        wedged = True
    # Somebody is already serving our port, and it isn't us: we have no PID.
    # Usually Docker's own immich-machine-learning container, sometimes an
    # older instance. Starting another is pointless, because the native
    # service now exits on a bind conflict rather than lingering, so the
    # watcher would relaunch it every tick forever. Immich has a working ML
    # endpoint either way, so say what is happening once and leave it alone.
    if not wedged and _ml_ping(config):
        # It answers and we have no PID. Ours with a lost pidfile, or somebody
        # else's service? Only the second is a reason to stand back.
        if adopt_live_ml(config) is not None:
            _ml_foreign_listener_warned = False
            return
        if not _ml_foreign_listener_warned:
            log.warning(
                "Port %s is already served by something this accelerator did not "
                "start (Docker's ML container?). Leaving it alone; stop that "
                "service, or set a different ml_port, if this Mac should serve ML.",
                config.get("ml_port", 3003),
            )
            _ml_foreign_listener_warned = True
        return
    _ml_foreign_listener_warned = False

    resolved_ml = _find_ml_dir()
    if resolved_ml and str(resolved_ml) != config.get("ml_dir"):
        config["ml_dir"] = str(resolved_ml)
        save_config(config)
    if not wedged:
        log.warning("ML service not running, attempting restart...")
    pid, engine = _start_ml_service(config)
    if pid:
        log.info("  ML restarted (PID %d, %s)", pid, engine)
    else:
        log.error("  ML restart failed")


def reconcile_components(config: dict) -> None:
    """Make the running state match the config for every component the caller
    is responsible for, except the worker.

    The worker is deliberately excluded: its health check is entangled with the
    fd-leak watchdog and the Docker version poll, so it stays inline in the
    watch loops. Everything else is a simple "should it be up" question and
    belongs in one enforcement point, so that editing config.json (the
    documented way to turn a component off) takes effect on a running install
    rather than only at the next start."""
    reconcile_dashboard()
    reconcile_ml(config)


def _find_ml_dir() -> Path | None:
    """Find the immich-ml-metal service directory. Sets up venv if needed.

    Candidate priority:
    1. Homebrew's stable opt symlink — survives ``brew upgrade`` because
       Homebrew maintains ``/opt/homebrew/opt/immich-accelerator`` as a
       symlink to the current Cellar version. The versioned Cellar path
       itself (e.g., ``.../Cellar/immich-accelerator/1.4.4/libexec/ml``)
       is ephemeral: ``brew upgrade`` deletes it, and ``config.json``
       references go stale (#29). The opt path doesn't have this problem.
    2. Relative to this file — works for direct git-clone installs where
       ``__file__`` lives at ``repo/immich_accelerator/__main__.py`` and
       ``ml/`` is a sibling at ``repo/ml/``.
    3. Home-directory fallback for legacy standalone ml clones.
    """
    candidates = [
        Path("/opt/homebrew/opt/immich-accelerator/libexec/ml"),
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


_STALE_WORKER_RE = _WORKER_CMD_RE  # same pattern, used by _kill_stale_processes + tests
_STALE_ML_RE = re.compile(r"(?:^|/)python[\d.]*\b.*\s-m\s+src\.main(?:\s|$)")


@contextlib.contextmanager
def _start_lock(timeout: float = 180.0):
    """Serialize cmd_start across processes.

    A component toggle runs cmd_start in the CLI process while the watcher, which
    reloads config every 30s, can see the same flip and run its own. Two
    concurrent starts race over the pid files, the /build link and the sharp
    rebuild. flock is advisory and process-scoped, which is exactly the scope
    needed: the loser waits for the winner rather than duplicating the work.

    Never fails the caller. A lock we cannot take is not a reason to refuse to
    start; it degrades to today's behavior.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = None
    try:
        fh = open(LOCK_FILE, "w")
    except OSError as e:
        log.debug("Could not open the start lock (%s); proceeding unlocked.", e)
        yield
        return
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning("Another start is still running; proceeding anyway.")
                    break
                time.sleep(0.5)
        yield
    finally:
        if locked:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _kill_stale_processes():
    """Kill any lingering immich worker or ML processes not tracked by PID files.

    Prevents zombie workers from competing for BullMQ jobs. This catches
    processes from previous runs, manual starts, or crashed accelerator
    instances that left orphans.

    History: an earlier version used ``pgrep -f "immich|src.main"``
    which matched ANY command line containing the substring "immich"
    — including the VM E2E harness's ``tart run immich-test-run-*``
    and ``docker compose ... immich-e2e-stack`` subprocesses, which
    the watchdog then SIGTERM'd mid-test. We couldn't reproduce the
    E2E failures until we realized it was our own code killing them.

    The fix walks `ps -axo pid,command` and filters in Python with
    proper regex (word boundaries, alternation, anchors) rather than
    trying to coax BSD pgrep's basic-regex flavor into matching
    `python -m src.main` without also matching `src.maintenance`.
    """
    stale = 0
    tracked_pids = set()
    for name in ("worker", "ml"):
        pid = read_pid(name)
        if pid:
            tracked_pids.add(pid)

    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return

    my_pid = os.getpid()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # `pid=,command=` prints the pid in the first whitespace-
        # separated field and the full command (possibly with spaces)
        # in the rest of the line.
        try:
            pid_str, cmdline = line.split(None, 1)
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == my_pid or pid in tracked_pids:
            continue
        if _STALE_WORKER_RE.search(cmdline) or _STALE_ML_RE.search(cmdline):
            try:
                os.kill(pid, signal.SIGTERM)
                stale += 1
            except OSError:
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


def _start_without_worker(config: dict, args) -> None:
    """cmd_start's worker-free counterpart: bring up the ML engine (and the
    dashboard) as a network-reachable compute node. No Docker, no DB/Redis,
    no worker, no /build link — none of that is relevant when this Mac's
    only job is to answer /predict for some other Immich instance's "Remote
    Machine Learning URL". Uses _start_ml_service (blocks on the health
    check) rather than cmd_start's split preferred/verify dance: unlike the
    worker path, there is no worker startup here to avoid delaying.
    """
    _kill_stale_processes()

    # Stop a worker that is still running despite being switched off.
    # _kill_stale_processes deliberately spares anything listed in a pidfile,
    # so a live tracked worker walks straight through it. This path used to be
    # ml-only-preset-only, where a worker could not exist; now that any full
    # install can reach it, skipping this leaves the worker pulling jobs while
    # `status`, the menu bar and the dashboard all report it off. Two workers
    # competing over one queue with nothing in any UI admitting the second one
    # exists is a worse failure than the one the toggle was meant to fix.
    if read_pid("worker"):
        log.info("Worker disabled in config, stopping it.")
        kill_pid("worker")

    if not _component_enabled("ml", config):
        # Stop a leftover engine first. The worker path does this too; without it
        # here, an ml-only box that turns ML off keeps serving /predict from a
        # process nothing believes is running.
        if read_pid("ml"):
            log.info("ML disabled in config, stopping it.")
            kill_pid("ml")
        # worker off + ml off. The dashboard alone is a legal, if odd, config
        # (watching a library some other machine processes), so honor it rather
        # than refusing. Nothing at all is a mistake worth naming.
        if not _component_enabled("dashboard", config):
            log.error("Every component is disabled, so there is nothing to start.")
            log.error("  Re-enable one: immich-accelerator component worker on")
            return
        log.info("Worker and ML are both disabled; starting the dashboard only.")
        start_dashboard()
        return

    ml_pid = read_pid("ml")
    if ml_pid:
        if not args.force:
            log.info("Already running (PID %d). Use --force to restart.", ml_pid)
            return
        cmd_stop(None)

    # Re-resolve ml_dir every start — config["ml_dir"] is a cache that goes
    # stale when brew upgrade deletes the old Cellar directory (#29).
    resolved_ml = _find_ml_dir()
    if resolved_ml and str(resolved_ml) != config.get("ml_dir"):
        log.info(
            "ML path changed (%s -> %s) — updating config.",
            config.get("ml_dir") or "(unset)",
            resolved_ml,
        )
        config["ml_dir"] = str(resolved_ml)
        save_config(config)

    ml_pid, ml_engine = _start_ml_service(config)
    if not ml_pid:
        log.warning("ML service did not start; the reason is above.")
        return
    log.info("  ML service ready (PID %d, %s)", ml_pid, ml_engine)

    # Bring up the dashboard too, so `start` is a complete bring-up.
    start_dashboard()

    log.info("")
    log.info("Immich Accelerator running (ML-only)")
    log.info("  ML log: %s/ml.log", LOG_DIR)


# Keys cmd_start dereferences directly on the worker path. Checked in one place,
# at the top, so a config that cannot support a worker produces a named error
# instead of a KeyError from 200 lines in. KeyError is not a RuntimeError, so an
# escaping one takes down the launchd watcher, which KeepAlive then relaunches
# into the same crash: that is why this is an explicit gate and not a try block.
_WORKER_CONFIG_KEYS = (
    "server_dir",
    "version",
    "node",
    "db_hostname",
    "db_port",
    "db_username",
    "db_name",
    "redis_hostname",
    "redis_port",
    "ml_port",
)


def _require_worker_config(config: dict) -> None:
    missing = [k for k in _WORKER_CONFIG_KEYS if not config.get(k)]
    if missing:
        raise RuntimeError(
            "This install has no worker configuration (missing: "
            + ", ".join(missing)
            + "). It was set up as an ML-only node, so there is nothing to "
            "start. Run: immich-accelerator setup"
        )


def _preflight_local(config: dict) -> bool:
    """Check this install against the Immich container on this Mac.

    Only for a local install, where that container really is the server.
    Returns False to refuse the start.
    """
    try:
        docker = _find_running_docker()
        immich = detect_immich(docker)
        if immich["workers_include"] != "api":
            log.error(
                "Docker is still running microservices. Two workers will conflict."
            )
            log.error("Set IMMICH_WORKERS_INCLUDE=api in docker-compose.yml first.")
            log.error("Run 'python -m immich_accelerator setup' for full instructions.")
            return False
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
            return False

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
        log.error("Could not read the local Immich container: %s", e)
        log.error("Start the Immich Docker stack, then try again.")
        return False
    return True


def _preflight_split(config: dict) -> bool:
    """Check a split install against the Immich it is configured to use.

    immich_url is what marks an install as split: setup --url is the only thing
    that writes it. A container on this Mac is not necessarily that server, so
    nothing here reads configuration out of one. Reading it compared this
    install against a stranger, refused the start over a mismatch that did not
    exist, and copied that stranger's database credentials into this config.
    Local Docker stays useful as a place to fetch server files from, and the
    version comes from the configured Immich's own API.

    The path mapping still has to be right, and the API can answer that. Returns
    False to refuse.
    """
    log.info(
        "Split install: validating path mapping against %s", config.get("immich_url")
    )
    api_key = config.get("api_key", "")
    upload_mount = config.get("upload_mount", "")
    if api_key and upload_mount:
        if _warn_on_path_mismatch(config.get("immich_url", ""), api_key, upload_mount):
            log.error("Refusing to start with a broken path mapping. Fix and retry.")
            return False
    else:
        log.warning(
            "Cannot check the path mapping: this split install has no api_key "
            "or no upload_mount set. Starting anyway; if thumbnails 404, that "
            "is the first thing to look at."
        )
    return True


def cmd_start(args):
    """Bring up the components this install has enabled.

    Serialized against every other start: a component toggle and the watcher can
    both decide to start the worker within the same 30s window, and two
    concurrent starts race over the pid files, the /build link and the sharp
    rebuild. The lock lives here rather than at the call sites because there are
    seven of them and the two that mattered were the two nobody remembered.
    """
    with _start_lock():
        _cmd_start(args)


def _cmd_start(args):
    config = load_config()

    if not _component_enabled("worker", config):
        _start_without_worker(config, args)
        return
    _require_worker_config(config)
    ml_enabled = _component_enabled("ml", config)

    # Kill any stale processes before starting
    _kill_stale_processes()

    # immich_url marks a split install: the Immich it names is authoritative,
    # and a container on this Mac is a different server that must not describe
    # this one. See docs/deployment.md.
    if config.get("immich_url"):
        if not _preflight_split(config):
            return
    elif not _preflight_local(config):
        return

    worker_pid = read_pid("worker")
    if worker_pid:
        if not args.force:
            log.info("Already running (PID %d). Use --force to restart.", worker_pid)
            return
        cmd_stop(None)

    # Re-resolve node every start. config["node"] is a cache, not the
    # source of truth — a user `brew upgrade` can silently swap node
    # underneath us, or delete the path entirely. find_node() enforces
    # SUPPORTED_NODE_MAJORS, so if the cached path points at something
    # that's been upgraded out of range, it'll be replaced here.
    try:
        node = find_node()
    except RuntimeError as e:
        log.error("%s", e)
        return
    if config.get("node") != node:
        log.info(
            "Node path changed (%s -> %s) — updating config.",
            config.get("node") or "(unset)",
            node,
        )
        config["node"] = node
        save_config(config)
    server_dir = config["server_dir"]

    # Node-version compatibility check against Immich's engines.node.
    # Catches the "brew upgrade bumped node past the supported LTS"
    # drift pattern with a clear message, before the worker crashes
    # mid-Nest-bootstrap with an opaque require('sharp') stack trace.
    ok, msg = _check_node_engines_compat(Path(server_dir), node)
    if not ok:
        log.error("Node version check failed: %s", msg)
        return

    # Sharp load preflight. Spawn `node -e "require('sharp')"` in the
    # server_dir — if it fails, try a rebuild and retry. If the retry
    # still fails, hard error with remediation. This turns a class of
    # opaque worker-crash bugs into a 1-second, clearly-labeled check.
    ok, err = _verify_sharp_loads(server_dir, node)
    if not ok:
        log.warning("Sharp failed to load — rebuilding against system libvips...")
        log.warning("  reason: %s", err.splitlines()[-1] if err else "(unknown)")
        try:
            _rebuild_sharp(Path(server_dir))
        except RuntimeError as e:
            log.error("%s", e)
            return
        ok, err = _verify_sharp_loads(server_dir, node)
        if not ok:
            log.error("Sharp still fails to load after rebuild:")
            for line in err.splitlines()[-10:]:
                log.error("  %s", line)
            log.error(
                "The worker cannot start without a working Sharp binding. "
                "If you just ran `brew upgrade`, revert to a supported node "
                "LTS: brew install node@22"
            )
            return

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
            "PATH": str(Path(node).parent) + ":" + os.environ.get("PATH", ""),
        }
    )

    # Point the worker at an ML engine. Normally that's our own on localhost.
    #
    # With ML off it has to be someone else's, and the env var must be actively
    # removed rather than merely not set: worker_env starts as a copy of our own
    # environment, so an inherited IMMICH_MACHINE_LEARNING_URL would otherwise
    # survive and silently point the worker at a port with nothing behind it.
    #
    # Deferring to Immich's own setting is only safe if the admin actually set
    # one. Immich's default is the Docker-internal hostname
    # `immich-machine-learning:3003`, which a worker running natively on macOS
    # cannot resolve, so every ML job would fail with ENOTFOUND and retry
    # forever. Say so loudly instead of pretending this is configured.
    if ml_enabled:
        worker_env["IMMICH_MACHINE_LEARNING_URL"] = (
            f"http://localhost:{config['ml_port']}"
        )
    elif config.get("ml_url"):
        worker_env["IMMICH_MACHINE_LEARNING_URL"] = config["ml_url"]
        log.info("ML is off here; the worker will use %s", config["ml_url"])
    else:
        worker_env.pop("IMMICH_MACHINE_LEARNING_URL", None)
        log.warning('ML is off here and no "ml_url" is set in %s.', CONFIG_FILE)
        log.warning("  Immich's own Machine Learning URL now governs. If it is")
        log.warning("  still the default (immich-machine-learning:3003), this")
        log.warning("  worker cannot resolve it and every ML job will fail.")
        log.warning('  Set "ml_url" to a reachable engine, or turn ML back on:')
        log.warning("    immich-accelerator component ml on")

    # Forward Redis credentials only when set — an empty password makes
    # ioredis send AUTH to a password-less Redis, which errors (issue #56).
    # REDIS_USERNAME enables ACL (user + password) auth on Redis 6+.
    if config.get("redis_username"):
        worker_env["REDIS_USERNAME"] = config["redis_username"]
    if config.get("redis_password"):
        worker_env["REDIS_PASSWORD"] = config["redis_password"]

    if config.get("upload_mount"):
        worker_env["IMMICH_MEDIA_LOCATION"] = config["upload_mount"]

    # pg_dump shim (issue #24): Immich hardcodes the Linux postgres
    # client path `/usr/lib/postgresql/<ver>/bin/pg_dump` in its
    # DatabaseBackupService. On macOS that path doesn't exist and
    # there's no env-var escape hatch in the upstream code. Instead
    # of patching Immich's source (which would break our "unmodified"
    # invariant), we preload a tiny Node module via `--require` that
    # monkey-patches child_process.spawn to rewrite that path to the
    # Homebrew libpq bin dir at call time. Immich's JS on disk is
    # never touched.
    # NODE_OPTIONS parsing reference (empirically verified with
    # Node 25.2, which matches the behavior back to 16+):
    #   unquoted    — splits on whitespace (fails for spaces)
    #   single '..' — FAILS, literals land in the filename (v1.4.2 bug)
    #   backslash \ — FAILS, Node does not honor shell-style escapes
    #   double  ".." — WORKS for all paths, with or without spaces
    #
    # So we wrap the shim path in double quotes unconditionally.
    # Brew Cellar paths are space-free in practice but double quotes
    # are still the portable correct form.
    shim_path = Path(__file__).parent / "hooks" / "pg_dump_shim.js"
    if shim_path.exists():
        existing = worker_env.get("NODE_OPTIONS", "").strip()
        require_arg = f'--require "{shim_path}"'
        worker_env["NODE_OPTIONS"] = (
            f"{existing} {require_arg}".strip() if existing else require_arg
        )

    # HEIC + camera-RAW decode shim (issues #62, #99): Sharp's prebuilt libvips
    # ships without an HEVC decoder (AVIF-only) and without a dcraw/libraw
    # loader, so iPhone HEICs and Canon/Nikon/Sony RAW files fail to decode and
    # never get thumbnails. This preload wraps the `sharp` module to route those
    # file paths through Homebrew libvips (to a lossless TIFF) before Sharp.
    # Same --require interposition; Immich's source on disk is untouched.
    heic_shim = Path(__file__).parent / "hooks" / "heic_decode_shim.js"
    if heic_shim.exists():
        existing = worker_env.get("NODE_OPTIONS", "").strip()
        require_arg = f'--require "{heic_shim}"'
        worker_env["NODE_OPTIONS"] = (
            f"{existing} {require_arg}".strip() if existing else require_arg
        )

    # pg keepalive shim (issue #74): in a split deployment a stateful firewall
    # between the worker and a remote Postgres can silently reap an idle
    # connection, after which the next read hangs to ETIMEDOUT and the worker
    # never recovers. Immich doesn't expose a keepalive env var, so we wrap the
    # `pg` module to set keepAlive on every connection. Same --require
    # interposition; Immich's source is untouched. No-op for same-host setups.
    pg_keepalive_shim = Path(__file__).parent / "hooks" / "pg_keepalive_shim.js"
    if pg_keepalive_shim.exists():
        existing = worker_env.get("NODE_OPTIONS", "").strip()
        require_arg = f'--require "{pg_keepalive_shim}"'
        worker_env["NODE_OPTIONS"] = (
            f"{existing} {require_arg}".strip() if existing else require_arg
        )

    # job retry shim: Immich hardcodes `attempts: 1` (no retry) for every
    # BullMQ queue. In a split deployment a transient connection drop to a
    # remote Postgres/Redis or a brief SMB hiccup permanently fails the job
    # instead of retrying. We wrap `bullmq`'s Queue constructor to raise the
    # attempt count with exponential backoff. Same --require interposition;
    # Immich's source is untouched.
    job_retry_shim = Path(__file__).parent / "hooks" / "job_retry_shim.js"
    if job_retry_shim.exists():
        existing = worker_env.get("NODE_OPTIONS", "").strip()
        require_arg = f'--require "{job_retry_shim}"'
        worker_env["NODE_OPTIONS"] = (
            f"{existing} {require_arg}".strip() if existing else require_arg
        )

    # /build link points to our build-data dir (set up during setup).
    # Required for Immich 2.7+ plugin WASM paths stored in the shared DB.
    build_data = DATA_DIR / "build-data"
    # Use the loose plugin-era check here, not _build_has_core_plugin: a
    # partially-extracted 3.0 plugin still needs the /build link and should hit
    # the clear "run setup" error below, not the pre-2.7 IMMICH_BUILD_DATA
    # fallback (which would let the worker start and fail at plugin-load time).
    has_plugins = _build_is_plugin_era(build_data)

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

    # Environment health checks — auto-detect and fix common issues
    # (ImageMagick HEIC codec, NFS mount, DB/Redis reachability).
    if not _preflight_env_health(config):
        return

    # Media-readiness gate (opt-out via "require_media_ready": false). Confirm
    # the media root is the real, mounted location before starting — otherwise
    # the worker can write thumbnails into a local placeholder that a network
    # mount later masks (silent data loss). Runs before the ML service starts so
    # a not-ready mount never leaves an orphaned ML process; the watch loop
    # retries, so the worker comes up as soon as the mount appears.
    if config.get("require_media_ready", True) and not ensure_media_ready(config):
        return

    # Re-resolve ml_dir every start. Same pattern as the node path
    # resolution above: config["ml_dir"] is a cache that goes stale
    # when brew upgrade deletes the old Cellar directory (#29).
    # _find_ml_dir() checks the stable /opt/homebrew/opt/ symlink
    # first, so it survives upgrades without config migration.
    resolved_ml = _find_ml_dir()
    if resolved_ml and str(resolved_ml) != config.get("ml_dir"):
        log.info(
            "ML path changed (%s -> %s) — updating config.",
            config.get("ml_dir") or "(unset)",
            resolved_ml,
        )
        config["ml_dir"] = str(resolved_ml)
        save_config(config)

    # Start ML service (native Swift engine by default, Python venv fallback).
    # Start the process now, but verify native health AFTER the worker/dashboard,
    # so a slow native cold start (loading weights + compiling shaders) never
    # holds up the worker.
    ml_started_here = False
    ml_native_pending = False
    ml_engine = None
    ml_pid = read_pid("ml")
    if not ml_enabled:
        # Worker on, ML off: this Mac does thumbnails and VideoToolbox while
        # some other machine answers /predict. Stop a leftover ML process so
        # the config is the truth, then leave it alone.
        if ml_pid:
            log.info("ML disabled in config, stopping it (PID %d).", ml_pid)
            kill_pid("ml")
            ml_pid = None
    elif not ml_pid and config.get("ml_dir"):
        ml_pid, ml_engine, ml_native_pending = _start_ml_preferred(config)
        if ml_pid:
            ml_started_here = True
            log.info("  ML service starting (PID %d, %s)", ml_pid, ml_engine)
        else:
            log.warning("ML service did not start; the reason is above.")
    elif ml_pid:
        log.info("ML service already running (PID %d)", ml_pid)
    elif not config.get("ml_dir"):
        log.warning("No ml_dir configured — ML service will not start.")
        log.warning("  Re-run: immich-accelerator setup")

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

    # Bring up the dashboard too, so `start` is a complete bring-up (#81 — users
    # shouldn't need `watch`/brew-services just to get the dashboard).
    start_dashboard()

    # Now that the worker and dashboard are up, verify the native ML engine
    # actually became healthy and fall back to the venv if not. Doing this here
    # (not before the worker) means a slow native cold start never delays the
    # worker; ML jobs simply queue and retry until the engine is serving.
    if ml_native_pending and ml_pid and ml_engine:
        ml_pid, ml_engine = _ml_verify_or_fallback(config, ml_pid, ml_engine)
        if ml_pid:
            log.info("  ML service ready (PID %d, %s)", ml_pid, ml_engine)

    # Reclaim disk from superseded server builds now that the worker is up on
    # the current version. Done here, not during extraction, so a still-running
    # old-version worker is never deleted out from under itself.
    _prune_old_server_versions(Path(server_dir).name)

    log.info("")
    log.info("Immich Accelerator running%s", "" if ml_enabled else " (worker only)")
    log.info("  Worker log: %s/worker.log", LOG_DIR)
    if ml_enabled:
        log.info("  ML log:     %s/ml.log", LOG_DIR)


def stop_all_fast() -> None:
    """Stop worker+ML+dashboard quickly, signalling ALL of them up front.

    For the watcher's SIGTERM handler: `brew services stop`/`restart` SIGKILLs
    the watcher only a few seconds after SIGTERM, which is less than cmd_stop's
    sequential per-service waits (up to 5s EACH) — so cmd_stop only ever killed
    the first service before being cut off (#81 follow-up: ML+dashboard survived
    a stop). Here every service gets SIGTERM in the first few milliseconds, so
    they all terminate even if the watcher is killed immediately after; the
    bounded wait + SIGKILL of stragglers is best-effort cleanup.
    """

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    pids: dict[str, int] = {}
    for name in ("worker", "ml", "dashboard"):
        pid = read_pid(name)
        if not pid:
            continue
        pids[name] = pid
        _signal_service(pid, signal.SIGTERM, name)
    _kill_all_worker_processes()  # sweep orphaned immich procs too

    # Short, PARALLEL wait (not 5s-per-service) so we stay well under launchd's
    # stop grace; SIGKILL anything still alive.
    for _ in range(30):  # up to ~3s total
        if all(not alive(p) for p in pids.values()):
            break
        time.sleep(0.1)
    # items(), not values(): `name` would otherwise be whatever the loop above
    # left bound, which is always "dashboard". The ownership check then compares
    # the worker's node command line against the dashboard's, decides the group
    # is not ours, and kills the worker by pid alone. Its ffmpeg and exiftool
    # survive the stop, which is the regression _signal_service exists to
    # prevent, on the escalation path rather than the first signal.
    for name, pid in pids.items():
        if alive(pid):
            _signal_service(pid, signal.SIGKILL, name)
    for name in pids:
        (PID_DIR / f"{name}.pid").unlink(missing_ok=True)
    if pids:
        log.info("Stopped: %s", ", ".join(pids))


def cmd_stop(_args):
    # An explicit stop is not a pause. Leaving the marker behind would have
    # status blame a missing library for a worker the user turned off.
    set_paused("")
    stopped = False
    for name in ("worker", "ml", "dashboard"):
        if kill_pid(name):
            log.info("%s stopped", name.capitalize())
            stopped = True
    if not stopped:
        log.info("Nothing running")


# Homebrew refuses to load a formula from a tap the user has not trusted, and
# that refusal is indistinguishable from "nothing to upgrade" to anything that
# only reads the exit status. `brew upgrade` then does nothing, `brew outdated`
# reports nothing, and the menu bar app offers nothing, so an install sits on
# an old version indefinitely with no signal anywhere. Measured on the release
# Mac by removing the tap from trust.json: `brew outdated --formula
# immich-accelerator` prints "Refusing to load formula ... from untrusted tap"
# and `brew info --json` returns no formula at all.
_TAP = "epheterson/immich-accelerator"


def _brew_path() -> str | None:
    """Where brew is, both places it can be.

    /opt/homebrew is Apple Silicon's prefix and /usr/local is the x86 one,
    which is a real configuration here: an x86 brew under Rosetta. Checking
    only the first is how a warning ends up unable to fire for the users who
    need it, which is indistinguishable from not having written it.
    """
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("brew")


def brew_refuses_our_tap() -> bool:
    """True when Homebrew will not load our formula because the tap is
    untrusted. False for any other reason, including brew not being installed:
    a non-brew install has nothing to say here."""
    brew = _brew_path()
    if not brew:
        return False
    try:
        out = subprocess.run(
            [brew, "outdated", "--formula", "immich-accelerator"],
            capture_output=True, text=True, timeout=10,
            # `brew outdated` is on Homebrew's auto-update path: once a day it
            # git-fetches every tap first. Measured at 68 seconds. `status` is
            # a question, not a maintenance task, and the run that paid that
            # cost was also the one that timed out and reported nothing.
            env={**os.environ,
                 "HOMEBREW_NO_AUTO_UPDATE": "1",
                 "HOMEBREW_NO_ENV_HINTS": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # On the line that names us: brew refuses for other untrusted taps too, and
    # matching anywhere in the output made someone else's tap print our fix.
    return any(
        "immich-accelerator" in line
        and ("untrusted tap" in line or "refusing to load formula" in line)
        # Joined with a newline, not concatenated: stdout's last line often has
        # no trailing newline, and gluing it to stderr's first makes a line
        # that can carry our formula name from one stream and the refusal from
        # the other, which is the cross-tap false positive this anchoring is
        # for.
        for line in "\n".join((out.stdout, out.stderr)).lower().splitlines()
    )


def cmd_status(_args):
    worker_pid = read_pid("worker")
    ml_pid = read_pid("ml")
    config = load_config() if CONFIG_FILE.exists() else {}

    paused = read_paused()
    if paused and paused.get("reason") == "backend-unreachable":
        log.warning("Worker:     paused, %s not answering", paused.get("detail") or "?")
        log.warning("            It starts again on its own when they are back.")
    elif paused and paused.get("reason") == "library-unreachable":
        log.warning(
            "Worker:     paused, library is not reachable at %s",
            paused.get("detail") or "?",
        )
        log.warning("            It starts again on its own when the mount is back.")

    if not worker_pid and not ml_pid:
        # "Disabled" and "stopped" are different facts, and a user who turned a
        # component off should never be told their install is broken.
        off = [c for c in COMPONENTS if not _component_enabled(c, config)]
        log.info("Not running%s", f" ({', '.join(off)} disabled)" if off else "")
        return

    def state(name: str, pid: int | None) -> str:
        if not _component_enabled(name, config):
            return "disabled"
        return f"running (PID {pid})" if pid else "stopped"

    log.info("Worker:     %s", state("worker", worker_pid))
    log.info("ML service: %s", state("ml", ml_pid))

    if config:
        log.info("Version:    %s", config.get("version", "?"))
        if config.get("ffmpeg_path"):
            log.info("FFmpeg:     %s (VideoToolbox)", config["ffmpeg_path"])

    if brew_refuses_our_tap():
        log.warning("Updates:    blocked. Homebrew will not load the formula "
                    "until the tap is trusted,")
        log.warning("            so `brew upgrade` silently leaves you on this "
                    "version. Run once:")
        log.warning("              brew trust %s", _TAP)


def cmd_logs(args):
    target = args.service or "worker"
    log_file = LOG_DIR / f"{target}.log"
    if not log_file.exists():
        print(f"No log file: {log_file}")
        return
    os.execvp("tail", ["tail", "-f", str(log_file)])


def cmd_update(_args):
    config = load_config()
    docker = _find_running_docker()
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


def int_setting(name: str, default: int, config: dict | None = None) -> int:
    """An integer knob, from config.json first and the environment second.

    The module-level constants below are bound at import, so a value in
    config.json could never reach them. On a Homebrew install the environment
    cannot be set at all (see config_env), which left these documented and
    unreachable.
    """
    # Same order as start_service: a real environment variable wins.
    if os.environ.get(name) is not None:
        return _int_env(name, default)
    raw = config_env(config).get(name)
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            log.warning("Ignoring %s=%r in config: not a whole number.", name, raw)
    return _int_env(name, default)


def _int_env(name: str, default: int) -> int:
    """Parse an int env var, falling back to default on a missing/bad value.

    Must never raise at import: these are module-level constants, so a bad
    value would otherwise break every subcommand, not just the watcher.
    """
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


# fd-leak safety net (#89): Immich leaks file handles processing some media
# (e.g. certain Sony XAVC files on an external drive), opening each source file
# and never closing it. Once the fd table gets huge, macOS starts failing
# spawn() with EBADF and the worker crashes. A healthy worker sits around 150
# open fds; restart it well before the wall. 0 disables the check.
FD_RESTART_THRESHOLD = _int_env("IMMICH_ACCEL_FD_RESTART_THRESHOLD", 10000)
# Minimum seconds between fd-triggered restarts, so a fast leak (or a high
# post-restart baseline) can't thrash the worker with back-to-back restarts.
FD_RESTART_COOLDOWN = _int_env("IMMICH_ACCEL_FD_RESTART_COOLDOWN", 300)


def _upgraded_on_disk() -> bool:
    """Did a `brew upgrade` land while we were running? If so, stop the now
    stale services and say yes; the caller returns and launchd KeepAlive
    relaunches watch with the new code.

    Both watch loops need this. It lived inline in the worker loop only, which
    meant that turning the worker off (now an ordinary supported choice, not
    just the ml-only preset) silently opted that install out of ever applying
    an upgrade: brew would write the new code and the KeepAlive'd watcher would
    keep executing the old one forever. That is #79 all over again, reachable
    from a documented command.

    No-op on non-Homebrew installs, where the opt symlink is absent and
    _installed_version falls back to __version__.
    """
    installed = _installed_version()  # read once (the symlink can change)
    if installed == __version__:
        return False
    log.info(
        "Accelerator upgraded on disk (%s -> %s) — relaunching.",
        __version__,
        installed,
    )
    cmd_stop(argparse.Namespace())
    return True


def _watch_without_worker(config: dict) -> str | None:
    """cmd_watch's worker-free counterpart: monitor and restart only the ML
    service (and dashboard), forever. No fd-leak watchdog (that leak is
    worker-only, #89) and no Docker/Immich-API polling (there is no local
    server_dir to re-extract when this box runs no worker).

    Returns _SWITCH if the worker was re-enabled mid-flight, so cmd_watch can
    hand over to the worker loop without this function knowing about it.
    """
    log.info("Watching ML-only service (Ctrl+C to stop)...")

    # `brew services stop`/`restart` send SIGTERM to this watcher. The
    # dashboard runs detached (own session) and would survive; an ML this
    # watcher started shares its process group and would be swept by launchd,
    # but only after this process exits, and an adopted ML is detached like the
    # dashboard. Trap the signal and stop them before exiting, matching
    # cmd_watch's behavior.
    def _graceful_stop(signum, _frame):
        log.info("Received signal %d, stopping services...", signum)
        try:
            stop_all_fast()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_stop)
    signal.signal(signal.SIGINT, _graceful_stop)

    if not read_pid("ml") and (
        not _component_enabled("ml", config) or adopt_live_ml(config) is None
    ):
        # Adoption first: a pidfile can be missing while the engine it named is
        # perfectly healthy, and starting a replacement in that state is how the
        # bind-conflict loop begins. reconcile_ml already knows this; the startup
        # path did not, so every watcher launch re-created the problem that
        # reconcile_ml then had to survive. Only when ML is enabled, matching
        # _watch_worker: with ML off, _start_without_worker is what stops a
        # leftover engine, and adopting one instead just delays that.
        log.info("Service not running, starting...")
        _start_without_worker(config, argparse.Namespace(force=True))
    else:
        reconcile_dashboard()

    # Say "the worker is on but cannot start" once, not every 30s forever.
    _warned_unstartable_worker = False

    while True:
        try:
            time.sleep(30)
            config = load_config()  # reload each cycle (setup may have changed it)

            # The worker can be switched back on while this loop runs. Hand
            # back to cmd_watch rather than sitting here ignoring it, but only
            # if a worker could actually start.
            #
            # `component worker on` refuses when the config has no worker
            # section, but these are documented as plain keys in config.json,
            # and a hand-edited or restored file reaches here without ever
            # passing that check. Handing over anyway means cmd_start raises
            # RuntimeError out of cmd_watch, main() exits 1, launchd relaunches
            # into the same crash, and the node loops forever with its ML
            # engine and dashboard unsupervised. Staying put keeps the engine
            # serving and names what is missing.
            if _component_enabled("worker", config):
                try:
                    _require_worker_config(config)
                except RuntimeError as e:
                    if not _warned_unstartable_worker:
                        log.error("%s", e)
                        log.error("  Staying on ML only; the engine keeps running.")
                        _warned_unstartable_worker = True
                else:
                    log.info("Worker enabled in config, switching to full watch.")
                    return _SWITCH
            else:
                _warned_unstartable_worker = False

            # Same as the worker loop: a `brew upgrade` must take effect here
            # too. Without this an install with the worker switched off never
            # applies an upgrade at all.
            if _upgraded_on_disk():
                return None

            # Keep service logs bounded, same as the worker path.
            cap_service_logs()

            # Match ML and the dashboard to config. Reloaded above each cycle,
            # so a toggle takes effect on a running install.
            reconcile_components(config)
        except KeyboardInterrupt:
            log.info("Watch stopped")
            return None


def cmd_watch(_args):
    """Monitor services and restart on crash. Detects Docker updates.

    Suitable for launchd KeepAlive — runs forever, checking every 30s.

    A thin dispatcher over two loops, because the worker changes what watching
    even means: with a worker there is an fd-leak watchdog, a Docker version
    poll and a server re-extract; without one there is none of that. Each loop
    hands back here when the "worker" component is toggled, so switching it
    takes effect on a running watcher instead of at the next restart.
    """
    global _SUPERVISING_ML
    _SUPERVISING_ML = True
    while True:
        config = load_config()
        if _component_enabled("worker", config):
            outcome = _watch_worker(config)
        else:
            outcome = _watch_without_worker(config)
        if outcome != _SWITCH:
            return


# Returned by a watch loop that wants cmd_watch to re-dispatch to the other one.
_SWITCH = "switch"


def _attempt_remount(config: dict, state: dict) -> None:
    """Try to put the library mount back, on a backoff, at most until the
    server tells us the credentials are wrong.

    `state` carries attempts/last/blocked across watch cycles. Once the server
    reports an auth failure we stop for good rather than retrying a password we
    now know is rejected: a background loop is exactly how an account ends up
    locked out, and unlike a network blip that is not something waiting fixes.
    """
    recipe = config.get("mount_recipe")
    if not recipe or state.get("blocked"):
        return

    attempts = state.get("attempts", 0)
    wait = _REMOUNT_BACKOFF[min(attempts, len(_REMOUNT_BACKOFF) - 1)]
    now = time.monotonic()
    last = state.get("last")
    if last is not None and now - last < wait:
        return

    state["attempts"] = attempts + 1
    state["last"] = now
    if is_mounted(recipe.get("mountpoint", "")):
        # The probe said unreachable but the mount is there, so this is a slow
        # or half-dead server, not a missing one. Mounting again would stack a
        # second mount on the same path and fix nothing.
        log.debug(
            "Mount point %s is still mounted; not remounting over it.",
            recipe.get("mountpoint"),
        )
        return

    log.info(
        "Trying to remount the library (%s from %s at %s)...",
        recipe.get("fstype"),
        recipe.get("spec"),
        recipe.get("mountpoint"),
    )
    ok, reason = remount(recipe)
    if ok:
        log.info("Remounted the library. Checking it on the next cycle.")
        state.clear()
        return
    if reason == "auth":
        state["blocked"] = True
        log.error(
            "The server rejected our credentials for %s, so we will not keep "
            "trying (repeated attempts can lock the account). Mount the share "
            "in Finder and save the password in the keychain, then the "
            "accelerator can do this on its own.",
            recipe.get("spec"),
        )
        return
    log.warning("  Remount failed: %s", reason)


def _watch_worker(config: dict) -> str | None:
    """cmd_watch's worker loop. Returns _SWITCH if the worker was disabled
    mid-flight, so cmd_watch can hand over to the worker-free loop."""
    log.info("Watching services (Ctrl+C to stop)...")

    fd_threshold = int_setting(
        "IMMICH_ACCEL_FD_RESTART_THRESHOLD", FD_RESTART_THRESHOLD, config
    )
    fd_cooldown = int_setting(
        "IMMICH_ACCEL_FD_RESTART_COOLDOWN", FD_RESTART_COOLDOWN, config
    )
    if fd_threshold > 0 and _LIBPROC is None:
        log.warning(
            "fd-leak watchdog (#89) inactive: libproc unavailable, cannot read "
            "worker fd counts on this system."
        )

    # `brew services stop`/`restart` send SIGTERM to this watcher, but the
    # worker and dashboard run detached (own session) and would survive — so a
    # plain stop/restart left them running (#81). An ML this watcher started
    # shares its process group, but launchd only sweeps that group after this
    # process exits, and an adopted ML is detached like the rest. Trap the
    # signal and stop the children before exiting, so the service lifecycle
    # works as expected.
    def _graceful_stop(signum, _frame):
        log.info("Received signal %d, stopping services...", signum)
        try:
            stop_all_fast()  # signal all up front — launchd SIGKILLs us in ~secs
        finally:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_stop)
    signal.signal(signal.SIGINT, _graceful_stop)

    # Loop-local rather than module state: it cannot leak between tests, and a
    # watcher restart is a clean slate.
    media_paused = False
    remount_state: dict = {}  # {attempts, last, blocked} for the remount backoff
    media_down_since: float | None = None  # first failed probe of a run
    media_io_countdown = 0  # cycles until the next advisory read of the mount
    backend_down_since: float | None = None  # first cycle Postgres/Redis went quiet
    backend_paused = False
    # This loop's state starts clean, so any marker from a previous run is
    # stale and would make status report a pause that is not happening.
    set_paused("")

    # A detached worker survives `brew services restart`, so a freshly-launched
    # watch (new code) would otherwise adopt a worker still running the OLD code
    # after a `brew upgrade`. If the running worker's stamped version doesn't
    # match ours, stop it so it gets restarted below with the new code (shims,
    # fixes). Version-gated so a plain crash relaunch doesn't churn a healthy
    # worker.
    if read_pid("worker"):
        try:
            worker_ver = WORKER_VERSION_FILE.read_text().strip()
        except OSError:
            worker_ver = None
        if worker_ver and worker_ver != __version__:
            log.info(
                "Worker is running stale code (%s, now %s) — restarting it.",
                worker_ver,
                __version__,
            )
            cmd_stop(argparse.Namespace())

    # First ensure everything is running. cmd_start brings up worker, ML, AND
    # the dashboard, so only start the dashboard separately when the worker/ML
    # were already up (avoids a double-start race on the dashboard). A missing
    # ML pid only counts when ML is enabled: otherwise every watcher restart
    # would read "ML is down" and bounce a perfectly healthy worker.
    ml_missing = (
        _component_enabled("ml", config)
        and not read_pid("ml")
        and adopt_live_ml(config) is None
    )
    if not read_pid("worker") or ml_missing:
        log.info("Services not running, starting...")
        cmd_start(argparse.Namespace(force=True))
    else:
        reconcile_components(config)

    # Warn if auto-update won't work for remote setups
    _watch_config = load_config()
    if _watch_config.get("immich_url") and not _watch_config.get("api_key"):
        log.warning("Auto-update disabled: immich_url is set but api_key is missing.")
        log.warning("  Add api_key to %s to enable version checking.", CONFIG_FILE)

    check_count = 0
    self_update_notified = False
    shown_worker_hints: set[str] = set()
    shown_media_warnings: set[str] = set()
    # None until the fd watchdog first restarts the worker. Compared against
    # time.monotonic() (uptime), so a 0.0 sentinel would wrongly read as "just
    # restarted" for the first FD_RESTART_COOLDOWN seconds after boot.
    last_fd_restart: float | None = None
    while True:
        try:
            time.sleep(30)
            config = load_config()  # reload each cycle (setup may have changed it)
            worker_handled = False  # set by the fd watchdog to skip crash-check

            # The worker can be switched off while this loop runs. Stop it and
            # hand back to cmd_watch, which re-dispatches to the worker-free
            # loop; otherwise the crash-check below would fight the toggle and
            # restart it every cycle.
            if not _component_enabled("worker", config):
                log.info("Worker disabled in config, switching to ML-only watch.")
                if read_pid("worker"):
                    kill_pid("worker")
                return _SWITCH

            if _upgraded_on_disk():
                return  # launchd KeepAlive relaunches with the new code

            # Keep service logs bounded — the worker can spew stack traces for
            # unsupported files; left unmanaged a log grew to 10GB.
            cap_service_logs()

            # Match ML and the dashboard to their config keys. config is
            # reloaded above each cycle, so editing config.json (the documented
            # way to turn one off) takes effect on a running install, and a
            # crashed one is restarted while it is enabled.
            reconcile_components(config)

            # The library can go away while we are running, and until now
            # nothing noticed. macOS drops network mounts on sleep or network
            # churn; Immich stores absolute paths, so every job then fails
            # instantly on ENOENT. The worker stayed up and shredded the queue
            # while the logs filled with errors that never named the cause.
            #
            # Pause instead. A stopped worker is honest and recovers by itself
            # the moment the mount returns, which is what a service running
            # unattended is for.
            # The worker cannot do anything without Postgres and Redis, and on
            # a split install those live on the same box as the library. When
            # that box goes away the worker used to sit there reconnecting all
            # night, filling its log with the same error, while status happily
            # reported it running.
            #
            # A connect is the right question to decide this on: it answers in
            # milliseconds and cannot hang, unlike the file read that made
            # 1.11.0 stop a healthy machine.
            down = backends_down(config)
            if down:
                if backend_down_since is None:
                    backend_down_since = time.monotonic()
                    log.warning(
                        "%s not answering. Leaving the worker alone until this "
                        "has lasted %ds.",
                        " and ".join(down),
                        int(MEDIA_UNREACHABLE_GRACE),
                    )
                if time.monotonic() - backend_down_since >= MEDIA_UNREACHABLE_GRACE:
                    if not backend_paused:
                        backend_paused = True
                        log.error(
                            "%s still not answering. Pausing the worker; it will "
                            "start again on its own when they are back.",
                            " and ".join(down),
                        )
                        set_paused("backend-unreachable", ", ".join(down))
                    if read_pid("worker"):
                        kill_pid("worker")
                    continue
            elif backend_down_since is not None:
                backend_down_since = None
                if backend_paused:
                    backend_paused = False
                    if media_paused:
                        # Two reasons to pause, one marker file. The library is
                        # still gone, so hand the marker back rather than delete
                        # it, and leave the worker down: the block below stops it
                        # again in this same cycle. Clearing it here left status
                        # saying "stopped" with no reason, for the whole
                        # remaining outage, which is the exact failure the marker
                        # exists to prevent.
                        log.info(
                            "Postgres and Redis are back, but the library mount "
                            "is still gone. Keeping the worker paused."
                        )
                        set_paused(
                            "library-unreachable", config.get("upload_mount") or ""
                        )
                    else:
                        log.info("Postgres and Redis are back. Starting the worker.")
                        set_paused("")
                        if not read_pid("worker"):
                            try:
                                cmd_start(argparse.Namespace(force=True))
                            except RuntimeError:
                                log.error(
                                    "  Worker start after the database returned failed"
                                )
                            worker_handled = True

            if config.get("require_media_ready", True):
                gone, point = library_mount_gone(config)
                if gone:
                    if media_down_since is None:
                        media_down_since = time.monotonic()
                        log.warning(
                            "The mount at %s that holds the library is no longer "
                            "mounted. Trying to put it back, and leaving the "
                            "worker alone until this has lasted %ds.",
                            point,
                            int(MEDIA_UNREACHABLE_GRACE),
                        )
                    # Immediately, not after the grace: a successful remount
                    # here means the worker is never stopped at all.
                    _attempt_remount(config, remount_state)
                    if time.monotonic() - media_down_since < MEDIA_UNREACHABLE_GRACE:
                        continue
                    if not media_paused:
                        media_paused = True
                        log.error(
                            "The mount at %s is still gone. Pausing the worker so "
                            "jobs are not failed against a missing library; it "
                            "will start again on its own when the mount is back.",
                            point,
                        )
                        set_paused(
                            "library-unreachable", config.get("upload_mount") or ""
                        )
                    if read_pid("worker"):
                        kill_pid("worker")
                    continue

                # Mounted. Whether we can actually read it is a separate
                # question, and deliberately not one that stops the worker: a
                # read can fail for permissions, a stalled server or a timeout,
                # and 1.11.0 stopped a healthy machine over exactly that. Say it
                # once so it can be diagnosed, then carry on.
                if media_down_since is not None or media_paused:
                    media_down_since = None
                    if media_paused:
                        media_paused = False
                        log.info("The mount at %s is back. Starting the worker.", point)
                        set_paused("")
                        if not read_pid("worker"):
                            try:
                                cmd_start(argparse.Namespace(force=True))
                            except RuntimeError:
                                log.error(
                                    "  Worker start after the mount returned failed"
                                )
                            worker_handled = True

                # This one reads a marker file on the mount, so unlike the
                # mount-table check above it is real I/O against the NAS. It is
                # advisory and never stops the worker, so it does not need to
                # run on every 30s cycle: once every ten of them is five
                # minutes, which is often enough for something nobody acts on
                # automatically. Skipped entirely when no worker is running,
                # because then nothing is reading the library anyway.
                media_io_countdown -= 1
                healthy, detail = True, ""
                if read_pid("worker") and media_io_countdown <= 0:
                    media_io_countdown = MEDIA_IO_EVERY
                    healthy, detail = media_io_healthy(config)
                if not healthy and detail and detail not in shown_media_warnings:
                    shown_media_warnings.add(detail)
                    log.warning(
                        "The library at %s is mounted but did not read cleanly: "
                        "%s. Leaving the worker running; jobs that touch it may "
                        "fail until this clears.",
                        config.get("upload_mount") or "?",
                        detail,
                    )

                # Record how the mount is put together while we can still see
                # it: this cannot be read back once it is gone, and it is what
                # tells us later that an absence is an absence.
                recipe = mount_recipe_for(config.get("upload_mount") or "")
                if recipe and recipe != config.get("mount_recipe"):
                    config["mount_recipe"] = recipe
                    save_config(config)
                    log.info(
                        "Recorded how the library is mounted (%s from %s), so it "
                        "can be remounted automatically if it drops.",
                        recipe["fstype"],
                        recipe["spec"],
                    )
                if remount_state:
                    remount_state.clear()

            # fd-leak safety net (#89): restart the worker before a runaway
            # open-file-descriptor count (an upstream Immich leak on some media)
            # exhausts the table and crashes it with `spawn EBADF`. Sum fds
            # across all worker processes (the leak may be in a sibling), and
            # cool down between restarts so a fast leak can't thrash.
            if fd_threshold > 0:
                fd_total = _worker_fd_total()
                if fd_total is not None and fd_total >= fd_threshold:
                    cooled = (
                        last_fd_restart is None
                        or time.monotonic() - last_fd_restart >= fd_cooldown
                    )
                    if cooled:
                        last_fd_restart = time.monotonic()
                        log.warning(
                            "Worker fd count %d exceeds %d: Immich leaks file "
                            "handles on some media (#89). Restarting the worker "
                            "before it exhausts the fd table and crashes.",
                            fd_total,
                            fd_threshold,
                        )
                        kill_pid("worker")
                        try:
                            cmd_start(argparse.Namespace(force=True))
                        except RuntimeError:
                            log.error("  Worker restart after fd leak failed")
                        # We handled the worker: skip the crash-check below so it
                        # can't also log "Worker crashed" and double-start this
                        # cycle. We do NOT `continue` (that would starve the
                        # update-detection cadence); if cmd_start early-returned
                        # without a live worker, next cycle's crash-check
                        # restarts it.
                        worker_handled = True
                    else:
                        log.debug(
                            "Worker fd count %d high but within restart "
                            "cooldown (%ds); not restarting yet.",
                            fd_total,
                            fd_cooldown,
                        )

            # Check worker
            if not worker_handled and not read_pid("worker"):
                # Surface a known unrecoverable cause once (e.g. a fresh
                # split-deploy geodata import failing) instead of looping
                # silently — restarting won't fix it until the user acts.
                hint = diagnose_worker_log(LOG_DIR / "worker.log")
                if hint and hint not in shown_worker_hints:
                    shown_worker_hints.add(hint)
                    log.error("Worker is failing to start:")
                    for line in hint.split("\n"):
                        log.error("  %s", line)
                log.warning("Worker crashed — restarting...")
                try:
                    cmd_start(argparse.Namespace(force=True))
                except RuntimeError:
                    log.error("  Worker restart failed, will retry in 30s")

            # Every 5 min, check if Immich updated. Skip on a cycle where the
            # fd watchdog just restarted the worker, so we don't tear it back
            # down (cmd_stop + re-extract) in the same tick; check_count stays
            # >=10 so it runs next cycle instead.
            check_count += 1
            if check_count >= 10 and not worker_handled:
                check_count = 0
                try:
                    cached = config.get("version", "").lstrip("v")
                    running = None

                    # Try local Docker first, fall back to Immich API
                    try:
                        docker = _find_running_docker()
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
                            docker = _find_running_docker()
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


def _watcher_running() -> bool:
    """Is a `watch` loop supervising the services?

    This is what makes stopping something safe. The watcher restarts a missing
    worker every 30s using the same path it uses for a crash, which is far
    better tested than anything a toggle could do inline, and it retries. When
    one is running, "stop it and let the supervisor bring it back" is the whole
    implementation of a restart.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", "immich_accelerator.*watch"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    pids = [int(p) for p in out.stdout.split() if p.isdigit()]
    return any(p != os.getpid() for p in pids)


def _restart_worker(reason: str) -> bool:
    """Restart the worker so it picks up a changed environment.

    The worker's ML URL is fixed when it spawns, so a change to it is only real
    after a restart. Stopping is always safe; starting is the part with ten
    failure paths. So when a supervisor is running we only stop, and let it do
    the start it already knows how to do. Unsupervised, we have to do both, and
    then we have to be honest that we might not get it back.
    """
    if not read_pid("worker"):
        return True  # nothing to restart
    log.info("Restarting the worker %s...", reason)
    kill_pid("worker")
    if _watcher_running():
        log.info("  The service watcher will bring it back within 30s.")
        return True
    cmd_start(argparse.Namespace(force=False))
    if not read_pid("worker"):
        log.error(
            "  The worker did not come back. Start it with: " "immich-accelerator start"
        )
        return False
    return True


def _warn_immich_still_points_here(config: dict) -> None:
    """Say that nothing else is configured to do machine learning.

    Only when there is genuinely nowhere else to go. `ml_url` is the engine
    the worker falls back to when ours is off (see the branch in the worker's
    environment setup), so on a two-Mac split it names the other Mac and
    everything is fine. Warning unconditionally told those users their search
    was about to break and pointed them at immich-machine-learning:3003, which
    would have broken the working configuration they had.

    Not a fix, a warning: setup does not edit anyone's docker-compose.yml and
    this must not either. "Nothing inside Docker is modified" is the promise on
    the front of the README, and quietly rewriting a URL in a file we say we
    never touch is worse than the problem.
    """
    if config.get("ml_url"):
        return  # somewhere else is configured; the worker will use it
    log.warning("Machine learning is off here and no other engine is set.")
    log.warning(
        "  Immich's own IMMICH_MACHINE_LEARNING_URL governs now. If it still "
        "points at this Mac, search, faces and text will fail."
    )
    log.warning(
        "  Point it back at your own container (usually "
        "http://immich-machine-learning:3003) and restart the Immich stack."
    )


def _set_component(name: str, on: bool) -> bool:
    """Record that a component should be on or off, and converge toward it.

    config.json is the intent and the single source of truth; this function's
    job is to make the running state agree with it. It deliberately does the
    cheap, safe half of that inline (stopping anything, starting ML or the
    dashboard) and delegates the expensive half (starting a worker) to the
    watcher when one is running, because cmd_start reports most of its ten
    failure modes by logging and returning, and a toggle is a bad place to
    discover that.

    Returns whether the component is now in, or is reliably converging toward,
    the requested state.
    """
    config = load_config()

    # Turning the worker on needs a config that describes one. An ml-only box
    # has never had one; say so before writing an intent we cannot honor.
    if name == "worker" and on:
        try:
            _require_worker_config(config)
        except RuntimeError as e:
            log.error("%s", e)
            return False

    config[name] = on
    # An explicit key beats the preset, but leaving a contradictory "ml_only"
    # behind is a trap for whoever reads the file next.
    if name == "worker" and on and config.pop("ml_only", None):
        log.info("Cleared the ml_only preset (the worker component is now on).")
    save_config(config)

    ok = True
    if name == "dashboard":
        # reconcile_dashboard reports what it actually did; re-reading the pid
        # here instead would race a just-spawned process.
        ok = bool(reconcile_dashboard())
    elif name == "ml":
        reconcile_ml(config)
        ok = bool(read_pid("ml")) == on
        # The worker's ML URL is baked in at spawn, so without this a live
        # worker keeps talking to an engine we just killed, failing every CLIP,
        # face and OCR job, or ignoring one we just started.
        ok = _restart_worker("to pick up the new ML setting") and ok
        if not on:
            _warn_immich_still_points_here(config)
    elif name == "worker":
        if not on:
            if read_pid("worker"):
                kill_pid("worker")
        elif _watcher_running():
            log.info("The service watcher will start the worker within 30s.")
        else:
            cmd_start(argparse.Namespace(force=False))
            # cmd_start signals most failures by logging and returning, so
            # whether a worker actually came up is the only usable signal.
            ok = bool(read_pid("worker"))
            if not ok:
                log.error("The worker did not start. See the errors above.")

    # Only on success. The menu bar shows the CLI's last output line as the
    # reason a toggle failed, and this sentence logged unconditionally, so
    # every failure banner in Settings read "Worker enabled. ML service and
    # Dashboard unaffected." while the real cause (Docker down, sharp broken,
    # media mount missing) scrolled past above it. Announcing success on the
    # way out of a failure is wrong on its own terms, too.
    if ok:
        others = " and ".join(COMPONENT_LABELS[c] for c in COMPONENTS if c != name)
        log.info(
            "%s %s. %s unaffected.",
            COMPONENT_LABELS[name],
            "enabled" if on else "disabled",
            others,
        )
    if not any(_component_enabled(c, config) for c in COMPONENTS):
        log.warning("Every component is now off, so nothing will run.")
    return ok


def cmd_component(args):
    """Turn a component on or off, or list what's enabled.

    The three components are the accelerator's three separable processes. This
    only goes as far as those process boundaries: video, thumbnails and RAW
    decode all run inside the single microservices worker, so which of those
    happen is Immich's job scheduler, not ours."""
    name = getattr(args, "name", None)
    state = getattr(args, "state", None)

    if not name:
        config = load_config()
        for component in COMPONENTS:
            enabled = _component_enabled(component, config)
            running = bool(read_pid(component))
            if not enabled:
                detail = "disabled"
            else:
                detail = "running" if running else "enabled, not running"
            log.info("  %-10s %s", component, detail)
        return

    if name not in COMPONENTS:
        log.error(
            "Unknown component '%s'. Choose one of: %s", name, ", ".join(COMPONENTS)
        )
        sys.exit(2)
    if state not in ("on", "off"):
        log.info("%s: %s", name, "on" if _component_enabled(name) else "off")
        return
    # Exit non-zero when the component did not reach the requested state. The
    # menu bar reads the exit code, and cmd_start reports most of its failures
    # by logging and returning, so without this a failed toggle renders as
    # success in the UI.
    if not _set_component(name, state == "on"):
        sys.exit(1)


def cmd_dashboard(args):
    """Run the web dashboard, or toggle it on/off.

    `dashboard on|off` is kept as an alias for `component dashboard on|off`,
    which is what it became in 1.9.0. With no state it runs the dashboard server
    in the foreground (what start_dashboard spawns in the background)."""
    state = getattr(args, "state", None)
    if state in ("on", "off"):
        if not _set_component("dashboard", state == "on"):
            sys.exit(1)
        return

    config = load_config()
    import importlib

    dashboard_mod = importlib.import_module(".dashboard", package=__package__)
    log.info("Starting dashboard on port %d...", args.port)
    dashboard_mod.run_dashboard(config, port=args.port)


# Immich's own transcode settings, so "stock" here means what a Docker Immich
# would have produced from the same input. preset ultrafast is what Immich sends,
# not veryfast: calibrating against the wrong preset is how a mapping ends up
# looking correct and measuring worse.
_STOCK_PRESET = "ultrafast"


def _ffprobe_duration(ffmpeg: str, path: str) -> float:
    # Only the last component. The shipped path is .../jellyfin-ffmpeg/ffmpeg,
    # so replacing every occurrence names a directory that does not exist.
    probe = str(Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe")))
    if not Path(probe).exists():
        return 0.0
    try:
        out = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def _encode_once(
    ffmpeg: str, src: str, dest: str, args: list[str]
) -> tuple[float, int, float]:
    """Encode and return (wall seconds, output bytes, cpu seconds).

    CPU time is the number that actually decides this. Wall time says which
    finishes one file first; CPU time says what the encode costs the rest of the
    machine, and this Mac is running Immich's other jobs and the ML engine at the
    same time. Zero bytes means it failed.
    """
    import resource

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    start = time.monotonic()
    try:
        r = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                src,
                *args,
                dest,
                "-y",
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0, 0, 0.0
    elapsed = time.monotonic() - start
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if r.returncode != 0:
        log.debug("encode failed: %s", (r.stderr or "").strip()[:300])
        return elapsed, 0, cpu
    try:
        return elapsed, Path(dest).stat().st_size, cpu
    except OSError:
        return elapsed, 0, cpu


def _ssim_against(ffmpeg: str, candidate: str, reference: str) -> float | None:
    """Mean SSIM of candidate against reference, or None if it could not be read."""
    try:
        r = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                # The mean is in the filter's summary line, which ssim logs at
                # info. error drops it, and stats_file=- writes the per-frame
                # numbers to stdout, so the last "All:" would be the last frame.
                "-loglevel",
                "info",
                "-i",
                candidate,
                "-i",
                reference,
                "-lavfi",
                "[0:v][1:v]ssim",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Anchored on the summary line, so a per-frame number can never be read as
    # the mean if the command above ever grows a stats file again.
    hit = re.search(r"SSIM .*\bAll:([0-9.]+)", r.stdout + r.stderr)
    return float(hit.group(1)) if hit else None


# The encoding switches, and the variable each one sets. The names live here
# rather than in the menu bar app so the CLI, the app and the tests cannot
# drift apart: the app calls this command, it does not write config.json.
#
# Each entry is (variable, what it does, what reads it). "what reads it" is not
# decoration: hardware video is honoured by ffmpeg-wrapper.sh, so its truthiness
# has to agree with the wrapper's own _off(), which test_encoding_switches pins
# by running the real script.
ENCODING_SWITCHES = {
    "hardware-video": (
        "IMMICH_ACCEL_HW_VIDEO",
        "Encode H.264 and HEVC with VideoToolbox",
    ),
    "hardware-decode": (
        "IMMICH_ACCEL_HW_DECODE",
        "Decode with VideoToolbox, including thumbnails and previews",
    ),
    "hardware-audio": (
        "IMMICH_ACCEL_HW_AUDIO",
        "Encode audio with AudioToolbox",
    ),
}

# Switches that are off unless asked for, because they change output. Everything
# else defaults on. Keeping the list here rather than in each reader means the
# CLI, the app and the wrapper cannot disagree about what "unset" means.
_DEFAULT_OFF = {"IMMICH_ACCEL_HW_AUDIO"}

# The positions on the one setting that matters: how far from Docker's output
# this install is willing to move. Each names every switch, so a preset is a
# complete statement rather than a diff against whatever was set before.
#
# Software is not "hardware off as a preference". It is the position where the
# ffmpeg wrapper passes Immich's arguments through untouched, so a library
# built here is byte-identical to one built by Docker and can move back.
#
# Hardware is what the hardware is measurably good at, not everything carrying a
# VideoToolbox name: hardware JPEG was measured slower than the software encoder
# and is deliberately absent.
ENCODING_PRESETS = {
    "software": {
        "IMMICH_ACCEL_HW_VIDEO": False,
        "IMMICH_ACCEL_HW_DECODE": False,
        "IMMICH_ACCEL_HW_AUDIO": False,
    },
    # Everything, including audio: this end means all of it. The cost is that
    # an install upgrading from before these settings existed has hardware
    # video without hardware audio, so it reads as custom until someone picks
    # an end. That is true rather than tidy, and one click fixes it.
    "hardware": {
        "IMMICH_ACCEL_HW_VIDEO": True,
        "IMMICH_ACCEL_HW_DECODE": True,
        "IMMICH_ACCEL_HW_AUDIO": True,
    },
}


PRESET_SUMMARY = {
    "software": "Transcode entirely in software",
    "hardware": "Transcode on the video hardware",
}

# Named for what they set, not for what they resemble. Calling the software end
# "Stock" would claim that an install matches Immich's container everywhere,
# and transcoding is only part of that: machine learning is chosen separately
# and is not part of this. A label that overclaims is worse than a plain one.
PRESET_DETAIL = {
    "software": (
        "Immich's own encoders. Byte for byte what Docker produces, except "
        "thumbnails for files ffmpeg cannot decode, which come from "
        "QuickLook. Most CPU."
    ),
    "hardware": (
        "VideoToolbox for decoding, video and audio. Much less CPU. Video "
        "looks identical to Docker's; audio and 10-bit thumbnails differ "
        "byte for byte."
    ),
    "custom": "Some on, some off. Set below.",
}


def encoding_preset(config: dict | None = None) -> str:
    """Which preset the current switches spell, or "custom".

    Derived rather than stored: a switch flipped from the CLI or set in the
    environment has to be reflected, and a stored name would go stale the moment
    it was. "custom" is a real answer, not a failure.
    """
    if config is None:
        try:
            config = load_config()
        except (RuntimeError, OSError, ValueError):
            config = {}
    for name, wanted in ENCODING_PRESETS.items():
        if not all(
            bool_setting(var, var not in _DEFAULT_OFF, config) is value
            for var, value in wanted.items()
        ):
            continue
        return name
    return "custom"


# The wrapper treats exactly these as off, and anything else (including an unset
# variable) as on. Parity with ffmpeg-wrapper.sh's _off() is the contract.
_ENV_OFF = ("0", "false", "no")
# The wrapper's _on(), for switches that are off unless asked for. Not the
# negation of _ENV_OFF: an unrecognised value leaves a default-on switch on and
# a default-off switch off, so in both cases a typo keeps the safer position
# rather than silently flipping behaviour.
_ENV_ON = ("1", "true", "yes")


def bool_setting(name: str, default: bool = True, config: dict | None = None) -> bool:
    """A boolean knob, from the environment first and config.json second.

    Same precedence as int_setting: a real environment variable wins, because
    someone who exported one is debugging and should not be overruled by a file.

    Truthiness follows the default, mirroring ffmpeg-wrapper.sh exactly: a
    default-on switch is off only for an explicit off word, and a default-off
    switch is on only for an explicit on word. test_fresh_install pins the two
    implementations against each other by running the real script.
    """
    raw = os.environ.get(name)
    if raw is None:
        raw = config_env(config).get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    return value not in _ENV_OFF if default else value in _ENV_ON


def encoding_switch_on(switch: str, config: dict | None = None) -> bool:
    """Whether one encoding switch is currently on.

    Most default on. The ones that change output default off (_DEFAULT_OFF),
    which is why an install that predates these settings reads as Custom rather
    than as either end: decoding and video were already on, audio was not, and
    saying so is better than claiming a position it never chose.
    """
    name, _ = ENCODING_SWITCHES[switch]
    return bool_setting(name, name not in _DEFAULT_OFF, config)


def apply_encoding_preset(name: str, config: dict) -> dict:
    """Write every switch a preset names. Returns the config, unsaved.

    Every switch, not the ones that differ: a preset has to be a complete
    statement, or moving from custom back to a named position would leave
    whatever was set by hand still in place under a name that denies it.
    """
    env = config.get("env")
    if not isinstance(env, dict):
        env = {}
    for var, value in ENCODING_PRESETS[name].items():
        env[var] = "1" if value else "0"
    config["env"] = env
    return config


def cmd_encoding(args):
    """Show or flip the encoding switches.

    These write config.json rather than the environment, because on a Homebrew
    install the environment is not reachable at all (see config_env). A real
    environment variable still wins at read time, so this reports what would
    actually take effect, not just what the file says.
    """
    switch = getattr(args, "switch", None)
    state = getattr(args, "state", None)
    config = load_config()

    # `encoding preset <name>` moves every switch at once. The word "preset"
    # cannot collide with a switch name: argparse restricts both to their own
    # choices, and the switch names all begin "hardware-".
    if switch == "preset":
        if state not in ENCODING_PRESETS:
            current = encoding_preset(config)
            log.info("Currently: %s", current)
            log.info("")
            for key, summary in PRESET_SUMMARY.items():
                log.info("  %s: %s", key, summary)
                log.info("    %s", PRESET_DETAIL[key])
            if current == "custom":
                log.info("  custom: %s", PRESET_DETAIL["custom"])
            return
        apply_encoding_preset(state, config)
        save_config(config)
        log.info("%s. %s.", state.capitalize(), PRESET_SUMMARY[state])
        for var in ENCODING_PRESETS[state]:
            if os.environ.get(var) is not None:
                log.warning(
                    "%s is set in the environment, and that wins over this.", var
                )
        log.info("Restart the accelerator for it to take effect.")
        return

    if not switch:
        log.info("Preset: %s", encoding_preset(config))
        for key, (name, description) in ENCODING_SWITCHES.items():
            on = encoding_switch_on(key, config)
            overridden = os.environ.get(name) is not None
            log.info(
                "  %-16s %-3s  %s%s",
                key,
                "on" if on else "off",
                description,
                (
                    " (set in the environment, which overrides config)"
                    if overridden
                    else ""
                ),
            )
        return

    name, _ = ENCODING_SWITCHES[switch]
    if state not in ("on", "off"):
        log.info(
            "%s: %s", switch, "on" if encoding_switch_on(switch, config) else "off"
        )
        return

    env = config.get("env")
    if not isinstance(env, dict):
        env = {}
    env[name] = "1" if state == "on" else "0"
    config["env"] = env
    save_config(config)

    log.info("%s is %s.", switch, state)
    # Worth saying, because otherwise the switch looks broken: the file changed
    # and the setting did not.
    if os.environ.get(name) is not None:
        log.warning(
            "%s is also set in the environment, and that wins. Unset it for this "
            "to take effect.",
            name,
        )
    log.info("Restart the accelerator for it to take effect.")


def _extract_frame(ffmpeg: str, video: str, dest: str, at: float) -> bool:
    """One frame as PNG, for looking at rather than measuring.

    PNG because a JPEG here would add its own artefacts on top of the ones the
    comparison is about, which is the fastest way to make a visual comparison
    lie.
    """
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{at:.2f}", "-i", video, "-frames:v", "1", dest],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and Path(dest).is_file()


def _comparison_page(rows: list[dict], out_dir: Path, source: str) -> Path:
    """A page with the numbers and the frames side by side.

    The numbers alone cannot answer "will I notice", and the frames alone
    cannot answer "what does it cost", so neither is much use without the
    other. Plain HTML with no external anything: it opens from a file:// URL
    on a Mac with no network and nothing to install.
    """
    from html import escape

    def cell(row: dict) -> str:
        img = (
            f'<img src="{row["frame"]}" alt="{row["label"]}">'
            if row.get("frame") else '<div class="noframe">no frame</div>'
        )
        ssim = f'{row["ssim"]:.6f}' if row.get("ssim") is not None else "n/a"
        return f"""<figure>
  {img}
  <figcaption>
    <strong>{escape(row["label"])}</strong>
    <dl>
      <dt>Wall</dt><dd>{row["wall"]:.1f}s</dd>
      <dt>CPU</dt><dd>{row["cpu"]:.1f}s ({row["cores"]:.1f} cores)</dd>
      <dt>Size</dt><dd>{row["mb"]:.1f} MB</dd>
      <dt>SSIM</dt><dd>{ssim}</dd>
    </dl>
  </figcaption>
</figure>"""

    body = "\n".join(cell(r) for r in rows)
    html = f"""<!doctype html>
<meta charset="utf-8">
<title>Encoder comparison: {escape(source)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 2rem auto;
         max-width: 68rem; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; margin-bottom: .2rem; }}
  p.sub {{ color: #888; margin-top: 0; }}
  .grid {{ display: grid; gap: 1.5rem;
           grid-template-columns: repeat(auto-fit, minmax(19rem, 1fr)); }}
  figure {{ margin: 0; }}
  img {{ width: 100%; border-radius: 6px; display: block; }}
  .noframe {{ aspect-ratio: 16/9; display: grid; place-items: center;
              background: #8883; border-radius: 6px; color: #888; }}
  figcaption {{ margin-top: .5rem; }}
  dl {{ display: grid; grid-template-columns: auto 1fr; gap: 0 .75rem;
        margin: .35rem 0 0; }}
  dt {{ color: #888; }}
  dd {{ margin: 0; font-variant-numeric: tabular-nums; }}
</style>
<h1>Encoder comparison</h1>
<p class="sub">{escape(source)} &middot; same frame from every encode, at full quality</p>
<div class="grid">
{body}
</div>
<p class="sub">SSIM is measured against the original. Higher is closer to the
source, not necessarily closer to what Immich would have produced: compare each
number against the software row, which is what Immich does on its own.</p>
"""
    page = out_dir / "comparison.html"
    page.write_text(html)
    return page


def cmd_compare(args):
    """Encode one of your files every way this Mac can, and show the results.

    encode-compare answers "which quality setting matches"; this answers the
    question before it, "what am I choosing between", with the numbers and a
    frame from each encode kept side by side. A person deciding where to sit
    between Stock and Apple Silicon cannot answer it from a table alone, and
    cannot answer it from pictures alone either.
    """
    preset = getattr(args, "preset", _STOCK_PRESET)
    src = str(Path(args.video).expanduser())
    if not Path(src).is_file():
        log.error("No such file: %s", src)
        return
    config = load_config() if CONFIG_FILE.exists() else {}
    ffmpeg = config.get("ffmpeg_path") or shutil.which("ffmpeg")
    if not ffmpeg or not Path(ffmpeg).exists():
        log.error("No ffmpeg found. Run setup, or pass one on PATH.")
        return

    out_dir = Path(args.output).expanduser() if args.output else (
        Path(tempfile.mkdtemp(prefix="immich-compare-"))
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = _ffprobe_duration(ffmpeg, src)
    # A frame from a quarter in, rather than the first: openings are often a
    # fade from black, where every encoder looks identical and perfect.
    at = (duration or 4.0) * 0.25

    log.info("Comparing on %s", Path(src).name)
    if duration:
        log.info("  %.0fs of video, CRF %d", duration, args.crf)
    log.info("")

    rows = []
    original = out_dir / "frame-original.png"
    has_original = _extract_frame(ffmpeg, src, str(original), at)

    # Software first: it is the reference every other row is judged against.
    # The label names the preset unless it is Immich's own, because a row
    # reading "what Immich does" while encoding at some other preset is a
    # caption that contradicts the numbers underneath it.
    software_label = (
        "Software (what Immich does)"
        if preset == _STOCK_PRESET
        else f"Software (x264 {preset})"
    )
    # The same arguments cmd_encode_compare uses, because a row captioned
    # "what Immich does" that omits them is not what Immich does: the pixel
    # format changes the encode, and leaving audio in makes the sizes on the
    # page incomparable between rows.
    plans = [(software_label, ["-c:v", "libx264", "-preset", preset,
                               "-crf", str(args.crf), "-pix_fmt", "yuv420p", "-an"])]
    for q in args.quality:
        plans.append((f"VideoToolbox q:v {q}",
                      ["-c:v", "h264_videotoolbox", "-q:v", str(q),
                       "-pix_fmt", "yuv420p", "-an"]))

    for label, encode_args in plans:
        dest = out_dir / f"{label.replace(' ', '-').replace(':', '').lower()}.mp4"
        secs, size, cpu = _encode_once(ffmpeg, src, str(dest), encode_args)
        if size == 0:
            log.warning("  %s: encode failed, skipped", label)
            continue
        ssim = _ssim_against(ffmpeg, str(dest), src)
        frame = out_dir / f"frame-{dest.stem}.png"
        got_frame = _extract_frame(ffmpeg, str(dest), str(frame), at)
        rows.append({
            "label": label,
            "wall": secs,
            "cpu": cpu,
            "cores": (cpu / secs) if secs else 0.0,
            "mb": size / (1024 * 1024),
            "ssim": ssim,
            "frame": frame.name if got_frame else None,
        })
        log.info("  %-28s %5.1fs wall  %5.1fs cpu  %6.1f MB  SSIM %s",
                 label, secs, cpu, size / (1024 * 1024),
                 f"{ssim:.6f}" if ssim is not None else "n/a")

    if not rows:
        log.error("Nothing encoded, so there is nothing to compare.")
        return
    # The page tells the reader to judge every row against the software one.
    # Carrying on without it produces a comparison against something absent.
    if not any(r["label"].startswith("Software") for r in rows):
        log.error(
            "The software encode failed, and every other row is only meaningful "
            "next to it. Nothing written."
        )
        return

    if has_original:
        # Labelled as the whole file, because that is what it measures: every
        # encode below strips audio with -an, so putting the source's total
        # size in the same column would overstate what the encodes saved.
        rows.insert(0, {
            "label": "Original (whole file, with audio)",
            "wall": 0.0, "cpu": 0.0, "cores": 0.0,
            "mb": Path(src).stat().st_size / (1024 * 1024), "ssim": None,
            "frame": original.name,
        })

    page = _comparison_page(rows, out_dir, Path(src).name)
    log.info("")
    log.info("Wrote %s", page)
    log.info("  The videos and frames are beside it, so you can look at them.")
    if not args.no_open and shutil.which("open"):
        subprocess.run(["open", str(page)], capture_output=True)


def cmd_encode_compare(args):
    """Transcode one file the way Immich would, and the way this Mac would.

    The point is to answer two questions with numbers from your own footage
    rather than from someone else's: how much faster the hardware encoder is,
    and what quality setting makes its output match what Immich would have
    produced on its own.
    """
    # getattr, because argparse always sets this and a caller building the
    # namespace by hand (the tests do) reasonably does not.
    preset = getattr(args, "preset", _STOCK_PRESET)
    src = str(Path(args.video).expanduser())
    if not Path(src).is_file():
        log.error("No such file: %s", src)
        return
    config = load_config() if CONFIG_FILE.exists() else {}
    ffmpeg = config.get("ffmpeg_path") or shutil.which("ffmpeg")
    if not ffmpeg or not Path(ffmpeg).exists():
        log.error("No ffmpeg found. Run setup, or pass one on PATH.")
        return

    duration = _ffprobe_duration(ffmpeg, src)
    log.info("Comparing encoders on %s", Path(src).name)
    if duration:
        log.info("  %.1fs of video, CRF %d", duration, args.crf)
    log.info("")

    with tempfile.TemporaryDirectory(prefix="immich-encode-compare-") as tmp:
        stock = str(Path(tmp) / "stock.mp4")
        secs, size, cpu = _encode_once(
            ffmpeg,
            src,
            stock,
            [
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(args.crf),
                "-pix_fmt",
                "yuv420p",
                "-an",
            ],
        )
        if not size:
            log.error(
                "The software encode failed, so there is nothing to compare against."
            )
            return
        stock_ssim = _ssim_against(ffmpeg, stock, src)
        log.info(
            "Software, as Immich would do it"
            if preset == _STOCK_PRESET
            else f"Software, x264 {preset}"
        )
        log.info("  x264 preset %s, CRF %d", preset, args.crf)
        _report(secs, size, stock_ssim, duration, cpu)
        stock_cpu, stock_wall = cpu, secs
        log.info("")

        log.info("Hardware, VideoToolbox")
        best = None
        for q in args.quality:
            dest = str(Path(tmp) / f"vt{q}.mp4")
            secs, size, cpu = _encode_once(
                ffmpeg, src, dest, ["-c:v", "h264_videotoolbox", "-q:v", str(q), "-an"]
            )
            if not size:
                log.warning("  q:v %-3d encode failed", q)
                continue
            ssim = _ssim_against(ffmpeg, dest, src)
            log.info("  q:v %d", q)
            _report(secs, size, ssim, duration, cpu)
            if ssim is not None and stock_ssim is not None:
                gap = abs(ssim - stock_ssim)
                if best is None or gap < best[0]:
                    # The timings travel with the setting they belong to. Held
                    # outside the tuple they were whatever the last q:v in the
                    # sweep happened to cost, which is not the one recommended.
                    best = (gap, q, ssim, secs, cpu)

        log.info("")
        if best:
            log.info(
                "Closest to the software encode: q:v %d (SSIM %.6f against %.6f).",
                best[1],
                best[2],
                stock_ssim,
            )
            log.info(
                "  Quality is content dependent, so run this on footage of your own "
                "before trusting one number."
            )
            log.info("")
            # The tradeoff is rarely the one people expect. Immich's preset is
            # ultrafast, which is genuinely quick, so software often finishes one
            # file sooner. What hardware buys is the machine: it leaves the cores
            # for the other jobs and for the ML engine running beside it.
            hw_wall, hw_cpu = best[3], best[4]
            if stock_wall and hw_wall:
                faster = "software" if stock_wall < hw_wall else "hardware"
                log.info(
                    "At q:v %d on this file, %s finished sooner "
                    "(%.1fs against %.1fs).",
                    best[1],
                    faster,
                    min(stock_wall, hw_wall),
                    max(stock_wall, hw_wall),
                )
            if stock_cpu and hw_cpu:
                log.info(
                    "It used %.1fs of cpu against %.1fs, so it leaves the "
                    "machine free for the other jobs and for machine learning.",
                    hw_cpu,
                    stock_cpu,
                )
        else:
            log.warning("No hardware encode completed, so there is nothing to compare.")


def _report(
    secs: float, size: int, ssim: float | None, duration: float, cpu: float = 0.0
) -> None:
    speed = f", {duration / secs:.1f}x realtime" if duration and secs else ""
    log.info("    %6.1fs wall   %7.1f MB%s", secs, size / (1024 * 1024), speed)
    if cpu:
        cores = f" ({cpu / secs:.1f} cores busy)" if secs else ""
        log.info("    %6.1fs cpu%s", cpu, cores)
    if ssim is not None:
        log.info("    SSIM %.6f against the original", ssim)


def cmd_ml_test(_args):
    """End-to-end diagnostic for the native ML service.

    Exercises /health + real /predict calls for CLIP and OCR with a
    synthetic image and reports per-check pass/fail. On any failure
    tails the last 30 lines of the ml log so the user has an actionable
    signal instead of the opaque 500 Immich returns.

    Exit 0 on all pass, non-zero on any failure. Addresses issue #20:
    "ML service is up and healthy but every job handler fails for
    all URLs" — until now the only way to diagnose was to know to
    read ~/.immich-accelerator/logs/ml.log and interpret it.
    """
    import urllib.error
    import urllib.request

    try:
        config = load_config()
    except RuntimeError:
        config = {}
    ml_port = int(config.get("ml_port", 3003))
    base = f"http://localhost:{ml_port}"

    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn):
        try:
            msg = fn()
            log.info("  ✓ %s — %s", name, msg)
            results.append((name, True, msg))
        except Exception as e:
            log.error("  ✗ %s — %s", name, e)
            results.append((name, False, str(e)))

    log.info("Testing ML service at %s...", base)
    log.info("")

    def ping():
        with urllib.request.urlopen(f"{base}/ping", timeout=5) as r:
            body = r.read().decode().strip()
        if body != "pong":
            raise RuntimeError(f"unexpected response: {body!r}")
        return "reachable"

    def health():
        with urllib.request.urlopen(f"{base}/health", timeout=15) as r:
            data = json.loads(r.read())
        status = data.get("status", "unknown")
        checks = data.get("checks", {})
        # A check value is a failure only if it starts with "error" —
        # the ml service uses "error: <detail>" for real failures,
        # "ok" for normal healthy state, and "active" for stub mode.
        # Anything else (including "active") is acceptable.
        failed = [
            k
            for k, v in checks.items()
            if isinstance(v, str) and v.lower().startswith("error")
        ]
        if failed:
            detail = ", ".join(f"{k}={checks[k]}" for k in failed)
            raise RuntimeError(f"status={status}, failing: {detail}")
        return f"status={status}, checks={list(checks.keys())}"

    def _tiny_jpeg() -> bytes:
        """10×10 solid-gray JPEG. Smallest valid test payload that
        every model backend accepts."""
        import base64

        return base64.b64decode(
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
            "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
            "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAKAAoDASIA"
            "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEB"
            "AQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABn/"
            "2Q=="
        )

    def predict(entries: dict, include_image: bool = True) -> bytes:
        """POST /predict multipart with entries JSON and optional image."""
        boundary = "----iac-ml-test"
        lines = []
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(b'Content-Disposition: form-data; name="entries"\r\n\r\n')
        lines.append(json.dumps(entries).encode() + b"\r\n")
        if include_image:
            lines.append(f"--{boundary}\r\n".encode())
            lines.append(
                b'Content-Disposition: form-data; name="image"; '
                b'filename="t.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
            )
            lines.append(_tiny_jpeg())
            lines.append(b"\r\n")
        lines.append(f"--{boundary}--\r\n".encode())
        body = b"".join(lines)

        req = urllib.request.Request(
            f"{base}/predict",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}")

    def clip_visual():
        data = predict({"clip": {"visual": {"modelName": "ViT-B-32__openai"}}})
        result = json.loads(data)
        # The upstream Immich ML wire format returns the embedding as
        # a JSON-string of a Python list (main.py:534 does
        # `str(embedding.tolist())`) — not a real JSON array.
        # Python list repr happens to be valid JSON for float lists,
        # so json.loads round-trips it safely.
        raw = result.get("clip")
        if isinstance(raw, str):
            try:
                emb = json.loads(raw)
            except ValueError as e:
                raise RuntimeError(f"could not parse embedding string: {e}")
        else:
            emb = raw
        if not isinstance(emb, list) or len(emb) < 100:
            size = len(emb) if hasattr(emb, "__len__") else "?"
            raise RuntimeError(f"unexpected embedding: {type(emb).__name__} len={size}")
        return f"embedding dim={len(emb)}"

    def ocr_check():
        data = predict(
            {
                "ocr": {
                    "detection": {"modelName": "default", "options": {}},
                    "recognition": {"modelName": "default", "options": {}},
                }
            }
        )
        result = json.loads(data)
        ocr = result.get("ocr", {})
        if not isinstance(ocr, dict) or "text" not in ocr:
            raise RuntimeError(f"unexpected ocr shape: {ocr}")
        return f"text items={len(ocr.get('text', []))}"

    check("ping", ping)
    check("health", health)
    check("clip visual (ViT-B-32__openai)", clip_visual)
    check("ocr (Apple Vision)", ocr_check)

    # The CLIP check above deliberately uses a fixed model so the diagnostic is
    # cheap and always available. That confused a user into thinking a model
    # switch hadn't taken effect (#116), so say which model Immich actually
    # asks for, and be explicit that the probe above is not it.
    configured = _immich_clip_model(config)
    if configured:
        log.info("")
        log.info("Immich is configured to use CLIP model: %s", configured)
        if configured != "ViT-B-32__openai":
            log.info("  The check above always probes ViT-B-32__openai, so it does not")
            log.info(
                "  reflect your setting. %s is downloaded and loaded the first",
                configured,
            )
            log.info("  time Immich sends a Smart Search job (watch ml.log).")

    all_passed = all(ok for _, ok, _ in results)
    log.info("")
    if all_passed:
        log.info("ML service OK — %d/%d checks passed", len(results), len(results))
        return

    failed = [(n, e) for n, ok, e in results if not ok]
    log.error(
        "ML service FAILED — %d/%d checks failed",
        len(failed),
        len(results),
    )
    log.error("")
    log.error("Last 30 lines of ~/.immich-accelerator/logs/ml.log:")
    log.error("")
    ml_log = LOG_DIR / "ml.log"
    if ml_log.exists():
        try:
            tail = ml_log.read_text(errors="replace").splitlines()[-30:]
            for line in tail:
                log.error("    %s", line)
        except OSError as e:
            log.error("    (could not read %s: %s)", ml_log, e)
    else:
        log.error("    (%s does not exist — is the ML service running?)", ml_log)
        log.error("")
        log.error("    Try: immich-accelerator start")
    log.error("")
    log.error("Common root causes:")
    log.error("  - mlx-clip / mlx version mismatch → brew reinstall immich-accelerator")
    log.error(
        "  - partial HuggingFace cache → rm -rf ~/.cache/huggingface/hub/models--mlx-community--clip-vit-base-patch32"
    )
    log.error("  - stale model files → rm -rf ~/.immich-accelerator/ml/models")
    sys.exit(1)


# --- Main ---


def cmd_uninstall(_args):
    """Remove services, data, and launchd config."""
    plist = Path.home() / "Library" / "LaunchAgents" / "com.immich.accelerator.plist"
    is_brew_install = "/Cellar/immich-accelerator/" in str(Path(__file__).resolve())
    ml_venv = Path(__file__).parent.parent / "ml" / "venv"

    log.info("")
    log.info("This will remove:")
    log.info("  - Running services (worker, ML, dashboard)")
    if plist.exists():
        log.info("  - Launchd service (auto-start on login)")
    log.info("  - Accelerator data (~/.immich-accelerator)")
    if ml_venv.exists() and not is_brew_install:
        log.info("  - ML venv (./ml/venv)")
    if is_brew_install:
        log.info("")
        log.info("NOTE: Homebrew owns the ML venv and the installed binary —")
        log.info("      this command only cleans up runtime state. To fully")
        log.info("      remove the formula afterwards, run:")
        log.info("        brew services stop immich-accelerator")
        log.info("        brew uninstall immich-accelerator")
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

    # Remove data directory. A server container that previously ran as root
    # may have left root-owned files here; if so we stop and explain rather
    # than force-deleting (see _rmtree_or_explain).
    if DATA_DIR.exists():
        if _rmtree_or_explain(DATA_DIR, what="accelerator data"):
            log.info("Removed %s", DATA_DIR)

    # Remove ML venv — but only for direct clones. Deleting brew's
    # Cellar-owned venv would break the currently-running python and
    # leave a broken formula until `brew reinstall`.
    if ml_venv.exists() and not is_brew_install:
        if _rmtree_or_explain(ml_venv, what="ML venv"):
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
    setup_p.add_argument(
        "--ml-only",
        action="store_true",
        help="Set up this Mac as an ML-only network compute node (no worker, no DB)",
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
    dash_p.add_argument(
        "state",
        nargs="?",
        choices=["on", "off"],
        help="Enable or disable the dashboard (omit to run it in the foreground)",
    )
    dash_p.add_argument("--port", type=int, default=8420, help="Dashboard port")
    comp_p = sub.add_parser(
        "component", help="Turn worker / ml / dashboard on or off (omit args to list)"
    )
    comp_p.add_argument("name", nargs="?", choices=list(COMPONENTS), help="Component")
    comp_p.add_argument(
        "state", nargs="?", choices=["on", "off"], help="Enable or disable it"
    )
    sub.add_parser(
        "ml-test",
        help="Diagnose the ML service (health + CLIP + OCR round-trip)",
    )
    enc_p = sub.add_parser(
        "encoding", help="Turn hardware encoding on or off (omit args to list)"
    )
    enc_p.add_argument(
        "switch",
        nargs="?",
        choices=list(ENCODING_SWITCHES) + ["preset"],
        help="Which switch, or 'preset'",
    )
    enc_p.add_argument(
        "state",
        nargs="?",
        choices=["on", "off"] + list(ENCODING_PRESETS),
        help="on/off for a switch, or the preset name",
    )
    all_p = sub.add_parser(
        "compare",
        help="Encode one video every way this Mac can, with frames to look at",
    )
    all_p.add_argument("video", help="a video file of your own")
    all_p.add_argument(
        "--preset",
        default=_STOCK_PRESET,
        help=(
            "x264 preset for the software side (default %(default)s, Immich's "
            "own). Set this to whatever your Immich is configured for."
        ),
    )
    all_p.add_argument(
        "--crf", type=int, default=23, help="the CRF Immich is set to (default 23)"
    )
    all_p.add_argument(
        "--quality", type=int, nargs="+", default=[59, 75],
        help="VideoToolbox q:v values to include",
    )
    all_p.add_argument("--output", help="where to write the comparison")
    all_p.add_argument(
        "--no-open", action="store_true", help="do not open the page when done"
    )
    cmp_p = sub.add_parser(
        "encode-compare",
        help="Transcode one video both ways and report speed, size and quality",
    )
    cmp_p.add_argument("video", help="a video file to test with, ideally your own")
    cmp_p.add_argument(
        "--preset",
        default=_STOCK_PRESET,
        help=(
            "x264 preset for the software side (default %(default)s, Immich's "
            "own). Set this to whatever your Immich is configured for, or the "
            "comparison answers a question about a setting you do not use."
        ),
    )
    cmp_p.add_argument(
        "--crf",
        type=int,
        default=23,
        help="the CRF Immich is set to (default 23, Immich's own default)",
    )
    cmp_p.add_argument(
        "--quality",
        type=int,
        nargs="+",
        default=[59, 65, 75, 85],
        help="VideoToolbox q:v values to try",
    )
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
            "component": cmd_component,
            "ml-test": cmd_ml_test,
            "encoding": cmd_encoding,
            "compare": cmd_compare,
            "encode-compare": cmd_encode_compare,
            "uninstall": cmd_uninstall,
        }[args.command](args)
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
