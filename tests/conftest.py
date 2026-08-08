"""Shared fixtures for immich-accelerator tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


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

    LAUNCH_AGENTS_DIR is the same trap with worse consequences. `setup --yes`
    under pytest installed a live, KeepAlive'd launch agent into the real
    ~/Library/LaunchAgents and left it crash-looping every 10 seconds. That
    hole predates --yes: the prompt used to raise EOFError under pytest and
    default to no, so nothing was written and nobody noticed. Adding a flag
    that answers yes turned a dormant isolation gap into a live one.

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
    launch_agents = tmp_path / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)

    with patch.multiple(
        "immich_accelerator.__main__",
        DATA_DIR=data_dir,
        CONFIG_FILE=config_file,
        PID_DIR=pid_dir,
        LOG_DIR=log_dir,
        LOCK_FILE=lock_file,
        LAUNCH_AGENTS_DIR=launch_agents,
    ):
        yield {
            "data_dir": data_dir,
            "config_file": config_file,
            "pid_dir": pid_dir,
            "log_dir": log_dir,
            "lock_file": lock_file,
            "launch_agents": launch_agents,
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
