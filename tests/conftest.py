"""Shared fixtures for immich-accelerator tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_real_machine_reads(monkeypatch):
    """Stop tests seeing the accelerator that is running on this machine.

    The suite passed on a laptop that does not run the product and failed
    eight ways on the Mac that does, which is the worst possible split: the
    people most likely to run the tests are the ones actually using the thing.
    Both outside contributors had already started writing "pre-existing
    failures, unrelated" in their pull requests, and a suite that is red for
    everyone who runs the product teaches everyone to ignore red.

    Two routes in, both closed here. read_pid("worker") falls back to a global
    process scan and adopts a live production worker, so "no pid file" tests
    found one anyway. _ml_ping opens a real HTTP connection to localhost:3003
    and the real ML service answers, so "ML is down, restart it" tests saw it
    up. A test that wants either behaviour patches it back explicitly.
    """
    import immich_accelerator.__main__ as m

    monkeypatch.setattr(m, "_adopt_live_worker", lambda: None)
    monkeypatch.setattr(m, "_ml_ping", lambda *a, **k: False)
    yield


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Point every module-level path at a temp directory.

    Every one of these is bound at import time off the real home, so anything
    left unpatched here is a path from the test straight into the user's
    production install. LOCK_FILE is the one that bites hardest: cmd_start
    takes a real flock on it, the release process runs pytest on the same Mac
    that runs production (CLAUDE.md: the Mini is the release gate), and tests
    that drive the real cmd_start would otherwise contend with the live
    watcher for the production start lock. Either the test hangs on the 180s
    timeout, or the watcher's worker restart stalls for the length of the
    suite and Immich sits with no worker while the tests pass.

    Same class of trap as read_pid's global process scan: the fixture looks
    isolating and is not.
    """
    data_dir = tmp_path / ".immich-accelerator"
    data_dir.mkdir()
    pid_dir = data_dir / "pids"
    pid_dir.mkdir()
    log_dir = data_dir / "logs"
    log_dir.mkdir()
    config_file = data_dir / "config.json"
    lock_file = data_dir / "start.lock"
    synthetic_conf = tmp_path / "synthetic.d" / "immich-accelerator"
    synthetic_conf.parent.mkdir(parents=True, exist_ok=True)
    legacy_synthetic = tmp_path / "synthetic.conf"

    with patch.multiple(
        "immich_accelerator.__main__",
        DATA_DIR=data_dir,
        CONFIG_FILE=config_file,
        PID_DIR=pid_dir,
        LOG_DIR=log_dir,
        LOCK_FILE=lock_file,
        # A real install has this file, and two tests wrote to and removed the
        # user's actual /etc/synthetic.d entry.
        SYNTHETIC_CONF=synthetic_conf,
        LEGACY_SYNTHETIC_CONF=legacy_synthetic,
        # reconcile_ml's "has it been quiet too long" timer. Module state, so a
        # test that leaves it set decides the outcome of the next one; patching
        # it here means every test starts from "no silence recorded yet".
        _ml_unresponsive_since=None,
    ):
        yield {
            "data_dir": data_dir,
            "config_file": config_file,
            "pid_dir": pid_dir,
            "log_dir": log_dir,
            "lock_file": lock_file,
        }


@pytest.fixture
def sample_config():
    """A realistic config dict."""
    return {
        "version": "2.6.3",
        "server_dir": "/Users/test/.immich-accelerator/server/2.6.3",
        "node": "/opt/homebrew/bin/node",
        "db_hostname": "localhost",
        "db_port": "5432",
        "db_username": "postgres",
        "db_password": "secret",
        "db_name": "immich",
        "redis_hostname": "localhost",
        "redis_port": "6379",
        "upload_mount": "/Volumes/photos/upload",
        "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
        "ml_dir": "/Users/test/immich-ml-metal",
        "ml_port": 3003,
        "api_key": "test-api-key-123",
        "immich_url": "http://localhost:2283",
    }


@pytest.fixture
def saved_config(tmp_data_dir, sample_config):
    """Write sample_config to the temp config file and return it."""
    config_file = tmp_data_dir["config_file"]
    config_file.write_text(json.dumps(sample_config, indent=2))
    os.chmod(config_file, 0o600)
    return sample_config
