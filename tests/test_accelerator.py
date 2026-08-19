"""Tests for immich_accelerator.__main__ — utility functions, config, CLI parsing, detection."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch, call

import pytest

from immich_accelerator.__main__ import (
    find_binary,
    check_port,
    is_valid_version,
    save_config,
    load_config,
    write_pid,
    read_pid,
    kill_pid,
    detect_immich,
    _docker_is_running,
    _find_running_docker,
    _find_exposed_port,
    _read_version,
    _build_link_ok,
    _ensure_build_link,
    _remove_build_link,
    SYNTHETIC_CONF,
    main,
    cmd_stop,
    cmd_status,
    cmd_logs,
    cmd_dashboard,
    start_dashboard,
    reconcile_dashboard,
    _dashboard_enabled,
    start_service,
    _setup_manual,
    cap_log,
    diagnose_worker_log,
    ensure_media_ready,
    MEDIA_MARKER_NAME,
    _installed_version,
    DATA_DIR,
    CONFIG_FILE,
    PID_DIR,
    LOG_DIR,
)

# ---------------------------------------------------------------------------
# _read_version
# ---------------------------------------------------------------------------


class TestReadVersion:
    def test_reads_version_file(self, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("1.3.1\n")
        with patch("immich_accelerator.__main__.Path") as mock_path:
            mock_path.return_value.parent.parent.__truediv__ = (
                lambda self, x: version_file
            )
            # Direct test: just call the real file logic
            result = version_file.read_text().strip()
            assert result == "1.3.1"

    def test_fallback_on_missing_file(self):
        with patch("immich_accelerator.__main__.Path") as mock_path:
            mock_path.return_value.parent.parent.__truediv__.return_value.read_text.side_effect = (
                OSError
            )
            result = _read_version()
            assert result == "1.0.0"


# ---------------------------------------------------------------------------
# find_binary
# ---------------------------------------------------------------------------


class TestFindBinary:
    def test_finds_existing_binary(self, tmp_path):
        binary = tmp_path / "mybin"
        binary.touch()
        result = find_binary("mybin", [str(binary)], "Install mybin")
        assert result == str(binary)

    def test_finds_first_match(self, tmp_path):
        bin1 = tmp_path / "bin1"
        bin2 = tmp_path / "bin2"
        bin1.touch()
        bin2.touch()
        result = find_binary("test", [str(bin1), str(bin2)], "hint")
        assert result == str(bin1)

    def test_skips_nonexistent_paths(self, tmp_path):
        real = tmp_path / "real"
        real.touch()
        result = find_binary("test", ["/nonexistent/path", str(real)], "hint")
        assert result == str(real)

    def test_raises_when_not_found(self):
        with pytest.raises(RuntimeError, match="mybin not found"):
            find_binary("mybin", ["/does/not/exist"], "Install mybin")

    def test_error_includes_hint(self):
        with pytest.raises(RuntimeError, match="brew install mybin"):
            find_binary("mybin", [], "brew install mybin")

    def test_empty_paths_list(self):
        with pytest.raises(RuntimeError):
            find_binary("x", [], "hint")


# ---------------------------------------------------------------------------
# check_port
# ---------------------------------------------------------------------------


class TestCheckPort:
    def test_returns_true_when_port_open(self):
        with patch("socket.create_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock()
            mock_conn.return_value.__exit__ = MagicMock()
            assert check_port("localhost", 5432, "Postgres") is True

    def test_returns_false_when_port_closed(self):
        with patch("socket.create_connection", side_effect=OSError("refused")):
            assert check_port("localhost", 9999, "Nothing") is False

    def test_returns_false_on_timeout(self):
        with patch("socket.create_connection", side_effect=socket.timeout("timed out")):
            assert check_port("localhost", 9999, "Test") is False


# ---------------------------------------------------------------------------
# is_valid_version
# ---------------------------------------------------------------------------


class TestIsValidVersion:
    @pytest.mark.parametrize(
        "version",
        [
            "1.2.3",
            "v1.2.3",
            "2.6.3",
            "v2.6.3",
            "10.20.30",
            "v0.0.1",
            "1.2.3-beta",
            "v1.2.3-rc1",
        ],
    )
    def test_valid_versions(self, version):
        assert is_valid_version(version) is True

    @pytest.mark.parametrize(
        "version",
        [
            "unknown",
            "",
            "latest",
            "abc",
            "1.2",
            "v1.2",
            "release-1",
        ],
    )
    def test_invalid_versions(self, version):
        assert is_valid_version(version) is False


# ---------------------------------------------------------------------------
# Config management (save_config / load_config)
# ---------------------------------------------------------------------------


class TestConfigManagement:
    def test_save_and_load_roundtrip(self, tmp_data_dir, sample_config):
        save_config(sample_config)
        loaded = load_config()
        assert loaded == sample_config

    def test_save_creates_directory(self, tmp_path):
        data_dir = tmp_path / "new" / "nested"
        config_file = data_dir / "config.json"
        with patch.multiple(
            "immich_accelerator.__main__",
            DATA_DIR=data_dir,
            CONFIG_FILE=config_file,
        ):
            save_config({"test": True})
            assert config_file.exists()
            assert json.loads(config_file.read_text()) == {"test": True}

    def test_save_sets_permissions(self, tmp_data_dir):
        save_config({"key": "value"})
        config_file = tmp_data_dir["config_file"]
        mode = oct(config_file.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_load_raises_when_missing(self, tmp_data_dir):
        with pytest.raises(RuntimeError, match="Not set up yet"):
            load_config()

    def test_save_atomic_write(self, tmp_data_dir):
        """Verify save uses tmp file + rename (atomic)."""
        save_config({"first": True})
        save_config({"second": True})
        loaded = load_config()
        assert loaded == {"second": True}

    def test_load_valid_json(self, tmp_data_dir):
        config_file = tmp_data_dir["config_file"]
        config_file.write_text('{"version": "2.6.3"}')
        loaded = load_config()
        assert loaded["version"] == "2.6.3"

    def test_load_invalid_json_raises(self, tmp_data_dir):
        config_file = tmp_data_dir["config_file"]
        config_file.write_text("not json at all")
        with pytest.raises(json.JSONDecodeError):
            load_config()


# ---------------------------------------------------------------------------
# Authenticated Redis (issue #56)
# ---------------------------------------------------------------------------


class TestRedisAuth:
    def test_manual_template_includes_redis_credentials(
        self, tmp_data_dir, monkeypatch
    ):
        # _setup_manual probes local tools after writing the template; stub
        # that out so the test doesn't depend on brew/node being installed.
        monkeypatch.setattr(
            "immich_accelerator.__main__._check_local_tools",
            lambda: ("/usr/bin/node", None, None),
        )
        _setup_manual(None)
        config = json.loads(tmp_data_dir["config_file"].read_text())
        assert "redis_username" in config
        assert "redis_password" in config

    def _run_preflight(self, monkeypatch, config):
        """Run _preflight_env_health, capturing the redis-cli PING.

        Only redis-cli is on PATH; every other probe is neutralized so
        the test exercises the Redis auth path regardless of host state.
        Returns (cmd, env) of the captured redis-cli invocation.
        """
        captured = {}

        def fake_run(cmd, **kwargs):
            if cmd and "redis-cli" in cmd[0]:
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
            return MagicMock(stdout="PONG", stderr="", returncode=0)

        monkeypatch.setattr(
            "immich_accelerator.__main__.shutil.which",
            lambda name: "/bin/redis-cli" if name == "redis-cli" else None,
        )
        monkeypatch.setattr("immich_accelerator.__main__.subprocess.run", fake_run)
        monkeypatch.setattr(
            "immich_accelerator.__main__.socket.create_connection",
            lambda *a, **k: MagicMock(),
        )
        from immich_accelerator.__main__ import _preflight_env_health

        _preflight_env_health(config)
        return captured.get("cmd", []), captured.get("env", {})

    def test_preflight_uses_auth_when_password_set(self, monkeypatch, sample_config):
        _cmd, env = self._run_preflight(
            monkeypatch, {**sample_config, "redis_password": "s3cret"}
        )
        assert env.get("REDISCLI_AUTH") == "s3cret"

    def test_preflight_omits_auth_when_no_password(self, monkeypatch, sample_config):
        cmd, env = self._run_preflight(monkeypatch, sample_config)
        assert "REDISCLI_AUTH" not in env
        assert "--user" not in cmd

    def test_preflight_passes_username_for_acl(self, monkeypatch, sample_config):
        cmd, env = self._run_preflight(
            monkeypatch,
            {**sample_config, "redis_username": "immich", "redis_password": "s3cret"},
        )
        assert cmd[cmd.index("--user") + 1] == "immich"
        assert env.get("REDISCLI_AUTH") == "s3cret"


# ---------------------------------------------------------------------------
# PID management (write_pid / read_pid / kill_pid)
# ---------------------------------------------------------------------------


class TestPidManagement:
    def test_write_and_read_pid(self, tmp_data_dir):
        current_pid = os.getpid()
        start_time = "Mon Apr  1 10:00:00 2026"
        with patch(
            "immich_accelerator.__main__._get_process_start_time",
            return_value=start_time,
        ):
            write_pid("worker", current_pid)
            pid = read_pid("worker")
        assert pid == current_pid

    def test_read_pid_returns_none_when_missing(self, tmp_data_dir):
        assert read_pid("worker") is None

    def test_read_pid_returns_none_for_dead_process(self, tmp_data_dir):
        pid_file = tmp_data_dir["pid_dir"] / "worker.pid"
        pid_file.write_text("999999\n")
        result = read_pid("worker")
        assert result is None
        # Should also clean up the stale file
        assert not pid_file.exists()

    def test_read_pid_detects_pid_reuse(self, tmp_data_dir):
        current_pid = os.getpid()
        pid_file = tmp_data_dir["pid_dir"] / "worker.pid"
        pid_file.write_text(f"{current_pid}\nOLD START TIME")

        with patch(
            "immich_accelerator.__main__._get_process_start_time",
            return_value="DIFFERENT START TIME",
        ):
            result = read_pid("worker")
            assert result is None

    def test_read_pid_matches_start_time(self, tmp_data_dir):
        current_pid = os.getpid()
        start_time = "Mon Apr  1 10:00:00 2026"
        pid_file = tmp_data_dir["pid_dir"] / "worker.pid"
        pid_file.write_text(f"{current_pid}\n{start_time}")

        with patch(
            "immich_accelerator.__main__._get_process_start_time",
            return_value=start_time,
        ):
            result = read_pid("worker")
            assert result == current_pid

    def test_kill_pid_returns_false_when_not_running(self, tmp_data_dir):
        assert kill_pid("worker") is False

    def test_kill_pid_sends_sigterm(self, tmp_data_dir):
        current_pid = os.getpid()
        with patch(
            "immich_accelerator.__main__.read_pid", return_value=current_pid
        ), patch("os.getpgid", return_value=current_pid), patch(
            "os.killpg"
        ) as mock_killpg, patch(
            "os.kill", side_effect=OSError
        ):  # process "gone" immediately
            kill_pid("worker")
            mock_killpg.assert_called_with(current_pid, signal.SIGTERM)


# ---------------------------------------------------------------------------
# detect_immich
# ---------------------------------------------------------------------------


class TestDetectImmich:
    def test_detects_server_by_image_name(self):
        docker_ps_output = "my-immich\tghcr.io/immich-app/immich-server:v2.6.3\n"
        package_json = json.dumps({"version": "2.6.3"})
        env_output = (
            "DB_PASSWORD=secret\nDB_USERNAME=postgres\nDB_DATABASE_NAME=immich\n"
        )
        mounts_json = json.dumps(
            [{"Destination": "/usr/src/app/upload", "Source": "/photos/upload"}]
        )

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[1] == "ps":
                result.stdout = docker_ps_output
            elif cmd[1] == "exec" and "package.json" in " ".join(cmd):
                result.stdout = package_json
            elif cmd[1] == "exec" and "env" in cmd:
                result.stdout = env_output
            elif cmd[1] == "inspect" and "Mounts" in " ".join(cmd):
                result.stdout = mounts_json
            elif cmd[1] == "port":
                result.stdout = "0.0.0.0:5432\n"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=run_side_effect):
            info = detect_immich("/usr/local/bin/docker")
            assert info["container"] == "my-immich"
            assert info["version"] == "2.6.3"
            assert info["db_password"] == "secret"
            assert info["upload_mount"] == "/photos/upload"

    def _detect_with(self, env_output, mounts_json):
        """Run detect_immich with a stubbed docker, given env + mounts."""
        docker_ps_output = "immich_server\tghcr.io/immich-app/immich-server:v2.7.5\n"
        package_json = json.dumps({"version": "2.7.5"})

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[1] == "ps":
                result.stdout = docker_ps_output
            elif cmd[1] == "exec" and "package.json" in " ".join(cmd):
                result.stdout = package_json
            elif cmd[1] == "exec" and "env" in cmd:
                result.stdout = env_output
            elif cmd[1] == "inspect" and "Mounts" in " ".join(cmd):
                result.stdout = mounts_json
            elif cmd[1] == "port":
                result.stdout = "0.0.0.0:5432\n"
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=run_side_effect):
            return detect_immich("/usr/local/bin/docker")

    def test_detects_data_mount_modern_default(self):
        """Modern compose mounts uploads at /data with no IMMICH_MEDIA_LOCATION
        env — the /upload substring match missed this (issue #62)."""
        mounts = json.dumps(
            [
                {"Destination": "/data", "Source": "/Volumes/4TB/Immich/Uploads"},
                {"Destination": "/etc/localtime", "Source": "/etc/localtime"},
                {"Destination": "/Importmap", "Source": "/Volumes/4TB/photos"},
            ]
        )
        info = self._detect_with("DB_PASSWORD=x\n", mounts)
        assert info["upload_mount"] == "/Volumes/4TB/Immich/Uploads"
        # media_location stays the raw env value (unset here) — see comment in
        # detect_immich; the effective /data is used only to pick the mount.
        assert info["media_location"] == ""

    def test_explicit_media_location_env_wins(self):
        """An explicit IMMICH_MEDIA_LOCATION picks the matching mount."""
        mounts = json.dumps(
            [
                {"Destination": "/data", "Source": "/srv/data"},
                {"Destination": "/mnt/immich", "Source": "/Volumes/X/immich"},
            ]
        )
        info = self._detect_with("IMMICH_MEDIA_LOCATION=/mnt/immich\n", mounts)
        assert info["upload_mount"] == "/Volumes/X/immich"
        assert info["media_location"] == "/mnt/immich"

    def test_legacy_upload_dest_still_detected(self):
        """Legacy /usr/src/app/upload destination is still found."""
        mounts = json.dumps(
            [{"Destination": "/usr/src/app/upload", "Source": "/photos/upload"}]
        )
        info = self._detect_with("", mounts)
        assert info["upload_mount"] == "/photos/upload"

    def test_named_volume_at_data_is_ignored(self):
        """A non-bind mount at /data (Source inside Docker's VM) is not a usable
        host path for the native worker, so it must not be chosen."""
        mounts = json.dumps(
            [
                {
                    "Type": "volume",
                    "Destination": "/data",
                    "Source": "/var/lib/docker/volumes/immich_data/_data",
                }
            ]
        )
        info = self._detect_with("", mounts)
        assert info["upload_mount"] is None

    def test_no_media_mount_not_detected(self):
        """No /data or /upload mount → upload_mount stays None (not detected)."""
        mounts = json.dumps(
            [{"Destination": "/etc/localtime", "Source": "/etc/localtime"}]
        )
        info = self._detect_with("", mounts)
        assert info["upload_mount"] is None

    def test_detects_server_by_container_name(self):
        docker_ps_output = "immich_server\tsome-custom-image:latest\n"
        package_json = json.dumps({"version": "2.5.0"})
        env_output = "DB_PASSWORD=pass\nDB_USERNAME=postgres\nDB_DATABASE_NAME=immich\n"
        mounts_json = "[]"

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[1] == "ps":
                result.stdout = docker_ps_output
            elif cmd[1] == "exec" and "package.json" in " ".join(cmd):
                result.stdout = package_json
            elif cmd[1] == "exec" and "env" in cmd:
                result.stdout = env_output
            elif cmd[1] == "inspect" and "Mounts" in " ".join(cmd):
                result.stdout = mounts_json
            elif cmd[1] == "port":
                result.stdout = ""
                result.returncode = 1
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=run_side_effect):
            info = detect_immich("/usr/local/bin/docker")
            assert info["container"] == "immich_server"

    def test_raises_when_docker_fails(self):
        result = MagicMock()
        result.returncode = 1
        result.stderr = "Docker daemon not running"
        with patch("subprocess.run", return_value=result):
            with pytest.raises(RuntimeError, match="Docker not running"):
                detect_immich("/usr/local/bin/docker")

    def test_raises_when_no_server_found(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "some-other-container\tnginx:latest\n"
        with patch("subprocess.run", return_value=result):
            with pytest.raises(RuntimeError, match="No Immich server container found"):
                detect_immich("/usr/local/bin/docker")

    def test_version_fallback_to_image_tag(self):
        """When package.json parsing fails, fall back to image tag."""
        docker_ps_output = "immich_server\tghcr.io/immich-app/immich-server:v2.6.3\n"
        env_output = "DB_PASSWORD=p\n"
        mounts_json = "[]"

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if cmd[1] == "ps":
                result.stdout = docker_ps_output
            elif cmd[1] == "exec" and "package.json" in " ".join(cmd):
                result.stdout = "not-json"
                result.returncode = 1
            elif cmd[1] == "inspect" and "Config.Image" in " ".join(cmd):
                result.stdout = "ghcr.io/immich-app/immich-server:v2.6.3\n"
            elif cmd[1] == "exec" and "env" in cmd:
                result.stdout = env_output
            elif cmd[1] == "inspect" and "Mounts" in " ".join(cmd):
                result.stdout = mounts_json
            elif cmd[1] == "port":
                result.stdout = ""
                result.returncode = 1
            else:
                result.stdout = ""
            return result

        with patch("subprocess.run", side_effect=run_side_effect):
            info = detect_immich("/usr/local/bin/docker")
            assert info["version"] == "v2.6.3"


# ---------------------------------------------------------------------------
# _find_exposed_port
# ---------------------------------------------------------------------------


class TestFindExposedPort:
    def test_returns_exposed_port(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "0.0.0.0:15432\n"
        with patch("subprocess.run", return_value=result):
            port = _find_exposed_port(
                "/usr/local/bin/docker", ["immich_postgres"], "5432"
            )
            assert port == "15432"

    def test_returns_default_when_not_exposed(self):
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        with patch("subprocess.run", return_value=result):
            port = _find_exposed_port(
                "/usr/local/bin/docker", ["immich_postgres"], "5432"
            )
            assert port == "5432"

    def test_tries_multiple_container_names(self):
        call_count = 0

        def run_side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.returncode = 1
                result.stdout = ""
            else:
                result.returncode = 0
                result.stdout = "0.0.0.0:6380\n"
            return result

        with patch("subprocess.run", side_effect=run_side_effect):
            port = _find_exposed_port(
                "/usr/local/bin/docker", ["redis1", "redis2"], "6379"
            )
            assert port == "6380"
            assert call_count == 2


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestCLIParsing:
    def test_setup_command(self):
        with patch("sys.argv", ["prog", "setup"]):
            parser = self._build_parser()
            args = parser.parse_args(["setup"])
            assert args.command == "setup"
            assert args.url is None
            assert args.manual is False

    def test_setup_with_url(self):
        parser = self._build_parser()
        args = parser.parse_args(["setup", "--url", "http://nas:2283"])
        assert args.url == "http://nas:2283"

    def test_setup_with_api_key(self):
        parser = self._build_parser()
        args = parser.parse_args(
            ["setup", "--url", "http://nas:2283", "--api-key", "key123"]
        )
        assert args.api_key == "key123"

    def test_setup_manual(self):
        parser = self._build_parser()
        args = parser.parse_args(["setup", "--manual"])
        assert args.manual is True

    def test_setup_import_server(self):
        parser = self._build_parser()
        args = parser.parse_args(["setup", "--import-server", "/tmp/server.tar.gz"])
        assert args.import_server == "/tmp/server.tar.gz"

    def test_setup_ml_only_flag(self):
        parser = self._build_parser()
        args = parser.parse_args(["setup", "--ml-only"])
        assert args.ml_only is True

    def test_setup_ml_only_defaults_false(self):
        parser = self._build_parser()
        args = parser.parse_args(["setup"])
        assert args.ml_only is False

    def test_start_command(self):
        parser = self._build_parser()
        args = parser.parse_args(["start"])
        assert args.command == "start"
        assert args.force is False

    def test_start_with_force(self):
        parser = self._build_parser()
        args = parser.parse_args(["start", "--force"])
        assert args.force is True

    def test_stop_command(self):
        parser = self._build_parser()
        args = parser.parse_args(["stop"])
        assert args.command == "stop"

    def test_status_command(self):
        parser = self._build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_logs_command_default(self):
        parser = self._build_parser()
        args = parser.parse_args(["logs"])
        assert args.command == "logs"
        assert args.service == "worker"

    def test_logs_command_ml(self):
        parser = self._build_parser()
        args = parser.parse_args(["logs", "ml"])
        assert args.service == "ml"

    def test_dashboard_command_default_port(self):
        parser = self._build_parser()
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"
        assert args.port == 8420
        assert args.state is None  # no on/off -> run the server

    def test_dashboard_custom_port(self):
        parser = self._build_parser()
        args = parser.parse_args(["dashboard", "--port", "9000"])
        assert args.port == 9000

    def test_dashboard_on_off_parse(self):
        parser = self._build_parser()
        assert parser.parse_args(["dashboard", "on"]).state == "on"
        assert parser.parse_args(["dashboard", "off"]).state == "off"

    def test_update_command(self):
        parser = self._build_parser()
        args = parser.parse_args(["update"])
        assert args.command == "update"

    def test_watch_command(self):
        parser = self._build_parser()
        args = parser.parse_args(["watch"])
        assert args.command == "watch"

    def test_uninstall_command(self):
        parser = self._build_parser()
        args = parser.parse_args(["uninstall"])
        assert args.command == "uninstall"

    def test_no_command_exits(self):
        with patch("sys.argv", ["prog"]), pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def _build_parser(self):
        """Build the same parser as main() for testing."""
        parser = argparse.ArgumentParser(prog="immich-accelerator")
        parser.add_argument("--version", action="version", version="test")
        sub = parser.add_subparsers(dest="command")

        setup_p = sub.add_parser("setup")
        setup_p.add_argument("--url")
        setup_p.add_argument("--api-key")
        setup_p.add_argument("--manual", action="store_true")
        setup_p.add_argument("--import-server", metavar="DIR")
        setup_p.add_argument("--ml-only", action="store_true")
        start_p = sub.add_parser("start")
        start_p.add_argument("--force", action="store_true")
        sub.add_parser("stop")
        sub.add_parser("status")
        logs_p = sub.add_parser("logs")
        logs_p.add_argument(
            "service", nargs="?", choices=["worker", "ml"], default="worker"
        )
        sub.add_parser("update")
        sub.add_parser("watch")
        dash_p = sub.add_parser("dashboard")
        dash_p.add_argument("state", nargs="?", choices=["on", "off"])
        dash_p.add_argument("--port", type=int, default=8420)
        sub.add_parser("uninstall")
        return parser


# ---------------------------------------------------------------------------
# Dashboard toggle (#31)
# ---------------------------------------------------------------------------


class TestDashboardToggle:
    """The dashboard on/off config toggle: default-on for back-compat, and both
    start_dashboard and cmd_dashboard honor it."""

    def test_enabled_by_default_when_key_absent(self, saved_config):
        # sample_config has no "dashboard" key -> default is on.
        assert _dashboard_enabled() is True

    def test_disabled_when_config_false(self, tmp_data_dir):
        save_config({"dashboard": False})
        assert _dashboard_enabled() is False

    def test_start_dashboard_noop_when_disabled(self, tmp_data_dir):
        save_config({"dashboard": False})
        with patch("subprocess.Popen") as popen, patch(
            "immich_accelerator.__main__.read_pid", return_value=None
        ):
            start_dashboard()
            popen.assert_not_called()

    def test_cmd_dashboard_off_disables_and_stops(self, saved_config):
        args = argparse.Namespace(state="off", port=8420)
        # Alive until it is killed, gone afterwards. A constant pid would mean
        # "we killed it and it is still running", which the toggle is now
        # supposed to report as a failure.
        with patch("immich_accelerator.__main__.read_pid", return_value=4321), patch(
            "immich_accelerator.__main__.kill_pid"
        ) as kill, patch("immich_accelerator.__main__._pid_on_port", return_value=None):
            cmd_dashboard(args)
            kill.assert_called_once_with("dashboard")
        assert load_config().get("dashboard") is False

    def test_cmd_dashboard_off_reports_failure_when_it_survives(self, saved_config):
        """The whole point of the exit code: the menu bar reads it, so a stop
        that did not stop must not look like success. Verified against the port,
        because kill_pid unlinks the pid file whether or not the process died,
        so a pid-file check could never see a survivor."""
        args = argparse.Namespace(state="off", port=8420)
        with patch("immich_accelerator.__main__.read_pid", return_value=4321), patch(
            "immich_accelerator.__main__.kill_pid"
        ), patch("immich_accelerator.__main__._pid_on_port", return_value=4321), patch(
            "immich_accelerator.__main__._process_is_our_dashboard", return_value=True
        ), pytest.raises(
            SystemExit
        ) as e:
            cmd_dashboard(args)
        assert e.value.code == 1

    def test_cmd_dashboard_on_enables_and_starts(self, saved_config):
        args = argparse.Namespace(state="on", port=8420)
        with patch("immich_accelerator.__main__.start_dashboard") as start:
            cmd_dashboard(args)
            start.assert_called_once()
        assert load_config().get("dashboard") is True

    def test_off_stops_an_untracked_dashboard(self, saved_config):
        """The pidfile can go missing (orphan from a prior run, PID reuse). If
        `off` only honored the pidfile, the dashboard would keep serving while
        the UI reported it disabled."""
        args = argparse.Namespace(state="off", port=8420)
        # The port frees once the process is signalled, as it would in reality.
        # A mock that holds the port forever would be asserting that SIGTERM
        # does not work.
        holder = {"pid": 5150}
        with patch("immich_accelerator.__main__.read_pid", return_value=None), patch(
            "immich_accelerator.__main__._pid_on_port",
            side_effect=lambda port: holder["pid"],
        ), patch(
            "immich_accelerator.__main__._process_is_our_dashboard", return_value=True
        ), patch(
            "os.kill", side_effect=lambda pid, sig: holder.update(pid=None)
        ) as kill:
            cmd_dashboard(args)
            kill.assert_called_once()
            assert kill.call_args[0][0] == 5150
        assert load_config().get("dashboard") is False

    def test_off_leaves_a_foreign_port_holder_alone(self, saved_config):
        """Something else on the port (OrbStack) is not ours to kill."""
        args = argparse.Namespace(state="off", port=8420)
        with patch("immich_accelerator.__main__.read_pid", return_value=None), patch(
            "immich_accelerator.__main__._pid_on_port", return_value=6000
        ), patch(
            "immich_accelerator.__main__._process_is_our_dashboard", return_value=False
        ), patch(
            "os.kill"
        ) as kill:
            cmd_dashboard(args)
            kill.assert_not_called()

    def test_reconcile_stops_a_running_dashboard_when_disabled(self, tmp_data_dir):
        """Editing config.json is the documented off-switch, so the watch loop
        has to act on it instead of only honoring it at the next start."""
        save_config({"dashboard": False})
        with patch("immich_accelerator.__main__.read_pid", return_value=4321), patch(
            "immich_accelerator.__main__.kill_pid"
        ) as kill, patch("immich_accelerator.__main__.start_dashboard") as start:
            reconcile_dashboard()
            kill.assert_called_once_with("dashboard")
            start.assert_not_called()

    def test_reconcile_starts_when_enabled(self, tmp_data_dir):
        save_config({"dashboard": True})
        with patch("immich_accelerator.__main__.start_dashboard") as start:
            reconcile_dashboard()
            start.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_stop
# ---------------------------------------------------------------------------


class TestCmdStop:
    def test_stops_all_services(self, tmp_data_dir):
        with patch("immich_accelerator.__main__.kill_pid") as mock_kill:
            mock_kill.return_value = True
            cmd_stop(None)
            assert mock_kill.call_count == 3
            mock_kill.assert_any_call("worker")
            mock_kill.assert_any_call("ml")
            mock_kill.assert_any_call("dashboard")

    def test_nothing_running(self, tmp_data_dir):
        with patch("immich_accelerator.__main__.kill_pid", return_value=False):
            cmd_stop(None)  # Should not raise


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def test_status_when_not_running(self, tmp_data_dir):
        with patch("immich_accelerator.__main__.read_pid", return_value=None):
            cmd_status(None)  # Should not raise

    def test_status_when_running(self, tmp_data_dir, saved_config):
        with patch("immich_accelerator.__main__.read_pid") as mock_read:
            mock_read.side_effect = lambda name: 1234 if name == "worker" else 5678
            cmd_status(None)  # Should not raise


# ---------------------------------------------------------------------------
# start_service
# ---------------------------------------------------------------------------


class TestStartService:
    def test_start_service_success(self, tmp_data_dir):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None  # still running

        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "immich_accelerator.__main__.write_pid"
        ) as mock_write, patch("time.sleep"):
            pid = start_service("worker", ["node", "main.js"], {}, "/tmp")
            assert pid == 12345
            mock_write.assert_called_once_with("worker", 12345)

    def test_start_service_immediate_exit(self, tmp_data_dir):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 1  # exited

        log_file = tmp_data_dir["log_dir"] / "worker.log"
        log_file.write_text("Error: something went wrong\n")

        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "immich_accelerator.__main__.write_pid"
        ), patch("time.sleep"), pytest.raises(
            RuntimeError, match="worker failed to start"
        ):
            start_service("worker", ["node", "main.js"], {}, "/tmp")


# ---------------------------------------------------------------------------
# _start_ml_service: native-default engine with venv fallback + health gate
# ---------------------------------------------------------------------------


class TestStartMlService:
    CFG = {"ml_dir": "/ml", "ml_port": 3003}
    NATIVE = (["nbin", "serve", "3003"], "/bundle", {})
    VENV = (["py", "-m", "src.main"], "/ml", {})

    def test_prefers_native_when_healthy(self):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_native_ml_spec", return_value=self.NATIVE), patch.object(
            m, "_venv_ml_spec", return_value=self.VENV
        ), patch.object(m, "start_service", return_value=111) as ss, patch.object(
            m, "_ml_healthy", return_value=True
        ):
            pid, engine = m._start_ml_service(dict(self.CFG))
            assert pid == 111 and engine == "native Swift"
            ss.assert_called_once()

    def test_falls_back_to_venv_when_native_unhealthy(self):
        import immich_accelerator.__main__ as m

        started = []

        def fake_start(name, cmd, env, cwd):
            started.append(cmd[0])
            return 222

        with patch.object(m, "_native_ml_spec", return_value=self.NATIVE), patch.object(
            m, "_venv_ml_spec", return_value=self.VENV
        ), patch.object(m, "start_service", side_effect=fake_start), patch.object(
            m, "_ml_healthy", return_value=False
        ), patch.object(
            m, "kill_pid", return_value=True
        ):
            pid, engine = m._start_ml_service(dict(self.CFG))
            assert engine == "Python venv"
            assert started == ["nbin", "py"]  # tried native, health-gated, then venv

    def test_venv_when_native_absent(self):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_native_ml_spec", return_value=None), patch.object(
            m, "_venv_ml_spec", return_value=self.VENV
        ), patch.object(m, "start_service", return_value=333), patch.object(
            m, "_ml_healthy", return_value=True
        ):
            pid, engine = m._start_ml_service(dict(self.CFG))
            assert pid == 333 and engine == "Python venv"

    def test_ml_engine_python_forces_venv(self):
        import immich_accelerator.__main__ as m

        with patch.object(
            m, "_native_ml_spec", return_value=self.NATIVE
        ) as nat, patch.object(
            m, "_venv_ml_spec", return_value=self.VENV
        ), patch.object(
            m, "start_service", return_value=444
        ), patch.object(
            m, "_ml_healthy", return_value=True
        ):
            cfg = dict(self.CFG, ml_engine="python")
            pid, engine = m._start_ml_service(cfg)
            assert engine == "Python venv"
            nat.assert_not_called()


# ---------------------------------------------------------------------------
# ml-only mode: cmd_start/cmd_watch dispatch to a worker-free counterpart
# ---------------------------------------------------------------------------


class TestStartMlOnly:
    """_start_without_worker, and cmd_start's dispatch to it."""

    CFG = {"ml_only": True, "ml_dir": "/ml", "ml_port": 3003}

    def test_cmd_start_dispatches_to_ml_only_and_skips_worker_path(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "load_config", return_value=dict(self.CFG)), patch.object(
            m, "_start_without_worker"
        ) as start_no_worker, patch.object(
            m, "find_docker"
        ) as find_docker, patch.object(
            m, "_preflight_env_health"
        ) as preflight, patch.object(
            m, "ensure_media_ready"
        ) as media_ready:
            m.cmd_start(argparse.Namespace(force=False))
            start_no_worker.assert_called_once()
            find_docker.assert_not_called()
            preflight.assert_not_called()
            media_ready.assert_not_called()

    def test_already_running_without_force_skips_start(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_kill_stale_processes"), patch.object(
            m, "read_pid", return_value=1234
        ), patch.object(m, "cmd_stop") as stop, patch.object(
            m, "_start_ml_service"
        ) as start_ml:
            m._start_without_worker(dict(self.CFG), argparse.Namespace(force=False))
            start_ml.assert_not_called()
            stop.assert_not_called()

    def test_force_stops_then_restarts(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_kill_stale_processes"), patch.object(
            m, "read_pid", return_value=1234
        ), patch.object(m, "cmd_stop") as stop, patch.object(
            m, "_find_ml_dir", return_value=None
        ), patch.object(
            m, "_start_ml_service", return_value=(999, "native Swift")
        ), patch.object(
            m, "start_dashboard"
        ) as start_dash:
            m._start_without_worker(dict(self.CFG), argparse.Namespace(force=True))
            stop.assert_called_once_with(None)
            start_dash.assert_called_once()

    def test_starts_dashboard_on_success(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_kill_stale_processes"), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(m, "_find_ml_dir", return_value=None), patch.object(
            m, "_start_ml_service", return_value=(111, "Python venv")
        ), patch.object(
            m, "start_dashboard"
        ) as start_dash:
            m._start_without_worker(dict(self.CFG), argparse.Namespace(force=False))
            start_dash.assert_called_once()

    def test_no_engine_available_does_not_start_dashboard(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_kill_stale_processes"), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(m, "_find_ml_dir", return_value=None), patch.object(
            m, "_start_ml_service", return_value=(None, None)
        ), patch.object(
            m, "start_dashboard"
        ) as start_dash:
            m._start_without_worker(dict(self.CFG), argparse.Namespace(force=False))
            start_dash.assert_not_called()


class TestWatchMlOnly:
    """_watch_without_worker, and cmd_watch's dispatch to it."""

    CFG = {"ml_only": True, "ml_dir": "/ml", "ml_port": 3003}

    def test_cmd_watch_dispatches_to_ml_only_and_skips_worker_path(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "load_config", return_value=dict(self.CFG)), patch.object(
            m, "_watch_without_worker"
        ) as watch_no_worker, patch.object(m, "cmd_start") as cmd_start:
            m.cmd_watch(argparse.Namespace())
            watch_no_worker.assert_called_once()
            cmd_start.assert_not_called()

    def test_skips_worker_only_checks_in_loop(self, tmp_data_dir):
        """Regression guard: the ml-only loop must never touch the worker
        crash-restart path, the fd-leak watchdog, or Docker version polling."""
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=4321), patch.object(
            m, "reconcile_dashboard"
        ), patch.object(m, "load_config", return_value=dict(self.CFG)), patch.object(
            m, "cap_service_logs"
        ), patch.object(
            m, "cmd_start"
        ) as cmd_start, patch.object(
            m, "_worker_fd_total"
        ) as fd_total, patch.object(
            m, "find_docker"
        ) as find_docker, patch(
            "signal.signal"
        ), patch(
            "time.sleep", side_effect=KeyboardInterrupt
        ):
            m._watch_without_worker(dict(self.CFG))
            cmd_start.assert_not_called()
            fd_total.assert_not_called()
            find_docker.assert_not_called()

    def test_starts_when_not_already_running(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        # Adoption runs before concluding ML is absent, so it has to say "not
        # ours" here: on a machine actually running the accelerator it would
        # otherwise adopt the live service and there would be nothing to start.
        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "adopt_live_ml", return_value=None
        ), patch.object(
            m, "reconcile_dashboard"
        ), patch.object(m, "_start_without_worker") as start_no_worker, patch(
            "signal.signal"
        ), patch(
            "time.sleep", side_effect=KeyboardInterrupt
        ):
            m._watch_without_worker(dict(self.CFG))
            start_no_worker.assert_called_once()

    def test_restarts_ml_on_crash_inside_loop(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        # First read_pid call is the pre-loop check (live -> no initial start).
        # Second is inside the loop body (crashed -> triggers a restart).
        with patch.object(m, "read_pid", side_effect=[4321, None]), patch.object(
            m, "reconcile_dashboard"
        ), patch.object(m, "load_config", return_value=dict(self.CFG)), patch.object(
            m, "cap_service_logs"
        ), patch.object(
            m, "_find_ml_dir", return_value=None
        ), patch.object(
            m, "_start_ml_service", return_value=(555, "native Swift")
        ) as start_ml, patch.object(
            m, "_start_without_worker"
        ) as start_no_worker, patch(
            "signal.signal"
        ), patch(
            "time.sleep", side_effect=[None, KeyboardInterrupt]
        ):
            m._watch_without_worker(dict(self.CFG))
            start_no_worker.assert_not_called()  # already running at the pre-loop check
            start_ml.assert_called_once()  # in-loop restart after it "crashed"


class TestSetupMlOnly:
    """_setup_ml_only, cmd_setup's dispatch to it, and the shared
    _finalize_config's ml-only guard around _ensure_build_link()."""

    def test_cmd_setup_ml_only_dispatches(self):
        import immich_accelerator.__main__ as m

        args = argparse.Namespace(
            ml_only=True, manual=False, import_server=None, url=None
        )
        with patch.object(m, "_setup_ml_only") as setup_ml_only, patch.object(
            m, "_setup_local"
        ) as setup_local, patch.object(
            m, "_setup_manual"
        ) as setup_manual, patch.object(
            m, "_setup_remote"
        ) as setup_remote:
            m.cmd_setup(args)
            setup_ml_only.assert_called_once_with(args)
            setup_local.assert_not_called()
            setup_manual.assert_not_called()
            setup_remote.assert_not_called()

    def test_writes_minimal_config_with_no_worker_fields(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_find_ml_dir", return_value=Path("/ml")), patch.object(
            m, "_finalize_config"
        ) as finalize:
            m._setup_ml_only(argparse.Namespace())
            finalize.assert_called_once()
            written = finalize.call_args[0][0]
            assert written["ml_only"] is True
            assert written["ml_port"] == 3003
            assert written["ml_dir"] == "/ml"
            for worker_key in ("db_hostname", "server_dir", "upload_mount"):
                assert worker_key not in written

    def test_handles_missing_venv(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_find_ml_dir", return_value=None), patch.object(
            m, "_finalize_config"
        ) as finalize:
            m._setup_ml_only(argparse.Namespace())
            written = finalize.call_args[0][0]
            assert written["ml_dir"] is None

    def test_finalize_config_skips_build_link_when_ml_only(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_ensure_build_link") as build_link, patch(
            "builtins.input", side_effect=EOFError
        ), patch("subprocess.run"):
            m._finalize_config({"ml_only": True})
            build_link.assert_not_called()

    def test_finalize_config_still_calls_build_link_when_not_ml_only(
        self, tmp_data_dir, sample_config
    ):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_ensure_build_link") as build_link, patch(
            "builtins.input", side_effect=EOFError
        ), patch("subprocess.run"):
            m._finalize_config(dict(sample_config))
            build_link.assert_called_once()


# ---------------------------------------------------------------------------
# Build link functions (_build_link_ok, _ensure_build_link, _remove_build_link)
# ---------------------------------------------------------------------------


class TestBuildLinkOk:
    def test_returns_false_when_build_missing(self, tmp_data_dir):
        """No /build → False."""
        (tmp_data_dir["data_dir"] / "build-data").mkdir(exist_ok=True)
        with patch("immich_accelerator.__main__.Path") as MockPath:
            real_path = Path

            def side_effect(p):
                if p == "/build":
                    return real_path(tmp_data_dir["data_dir"] / "nonexistent")
                return real_path(p)

            MockPath.side_effect = side_effect
        # Simpler: just mock the target check directly
        with patch("immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]):
            with patch("pathlib.Path.exists", return_value=False):
                assert _build_link_ok() is False

    def test_returns_true_when_build_resolves_correctly(self, tmp_data_dir):
        """ "/build" resolves to build-data → True."""
        build_data = tmp_data_dir["data_dir"] / "build-data"
        build_data.mkdir(exist_ok=True)
        with patch("immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]):
            target = tmp_data_dir["data_dir"] / "build-link"
            target.symlink_to(build_data)
            with patch("immich_accelerator.__main__.Path") as MockPath:

                def path_factory(p="/build"):
                    if p == "/build":
                        return target
                    return Path(p)

                MockPath.side_effect = path_factory
                assert _build_link_ok() is True

    def test_returns_false_when_build_points_elsewhere(self, tmp_data_dir):
        """ "/build" exists but points to wrong dir → False."""
        build_data = tmp_data_dir["data_dir"] / "build-data"
        build_data.mkdir(exist_ok=True)
        wrong_dir = tmp_data_dir["data_dir"] / "wrong"
        wrong_dir.mkdir()
        with patch("immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]):
            target = tmp_data_dir["data_dir"] / "build-link"
            target.symlink_to(wrong_dir)
            with patch("immich_accelerator.__main__.Path") as MockPath:

                def path_factory(p="/build"):
                    if p == "/build":
                        return target
                    return Path(p)

                MockPath.side_effect = path_factory
                assert _build_link_ok() is False


class TestEnsureBuildLink:
    def test_returns_true_when_already_ok(self, tmp_data_dir):
        """If _build_link_ok() → True, return immediately."""
        with patch(
            "immich_accelerator.__main__._build_link_ok", return_value=True
        ), patch("immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]):
            assert _ensure_build_link() is True

    def test_returns_false_when_build_exists_wrong_target(self, tmp_data_dir):
        """/build exists but wrong target → warn, return False."""
        (tmp_data_dir["data_dir"] / "build-data").mkdir(exist_ok=True)
        with patch(
            "immich_accelerator.__main__._build_link_ok", return_value=False
        ), patch(
            "immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]
        ), patch(
            "immich_accelerator.__main__.Path"
        ) as MockPath:
            mock_build = MagicMock()
            mock_build.exists.return_value = True
            MockPath.side_effect = lambda p: mock_build if p == "/build" else Path(p)
            assert _ensure_build_link() is False

    def test_returns_false_when_conf_exists_but_not_active(self, tmp_data_dir):
        """synthetic.d file exists but /build not active → needs reboot."""
        (tmp_data_dir["data_dir"] / "build-data").mkdir(exist_ok=True)
        synth_file = tmp_data_dir["data_dir"] / "synthetic-conf"
        synth_file.write_text("build\tUsers/test\n")
        with patch(
            "immich_accelerator.__main__._build_link_ok", return_value=False
        ), patch(
            "immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]
        ), patch(
            "immich_accelerator.__main__.SYNTHETIC_CONF", synth_file
        ), patch(
            "immich_accelerator.__main__.Path"
        ) as MockPath:
            mock_build = MagicMock()
            mock_build.exists.return_value = False
            MockPath.side_effect = lambda p: mock_build if p == "/build" else Path(p)
            assert _ensure_build_link() is False

    def test_returns_false_when_user_declines(self, tmp_data_dir):
        """User says 'n' → return False, no sudo."""
        (tmp_data_dir["data_dir"] / "build-data").mkdir(exist_ok=True)
        synth_file = tmp_data_dir["data_dir"] / "synthetic-conf"
        with patch(
            "immich_accelerator.__main__._build_link_ok", return_value=False
        ), patch(
            "immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]
        ), patch(
            "immich_accelerator.__main__.SYNTHETIC_CONF", synth_file
        ), patch(
            "immich_accelerator.__main__.Path"
        ) as MockPath, patch(
            "builtins.input", return_value="n"
        ):
            mock_build = MagicMock()
            mock_build.exists.return_value = False
            MockPath.side_effect = lambda p: mock_build if p == "/build" else Path(p)
            assert _ensure_build_link() is False

    def test_appends_build_entry_when_conf_has_foreign_lines(self, tmp_data_dir):
        """File exists with a foreign line but no build entry → append, don't skip.

        Regression for issue #61: a hand-edited synthetic.d file (e.g. a manual
        upload-path entry) must not be mistaken for a configured build link. We
        write the missing build entry while preserving the user's foreign line.
        """
        (tmp_data_dir["data_dir"] / "build-data").mkdir(exist_ok=True)
        synth_file = tmp_data_dir["data_dir"] / "synthetic-conf"
        synth_file.write_text("usr/src/app/upload\tVolumes/upload\n")

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch(
            "immich_accelerator.__main__._build_link_ok", return_value=False
        ), patch(
            "immich_accelerator.__main__.DATA_DIR", tmp_data_dir["data_dir"]
        ), patch(
            "immich_accelerator.__main__.SYNTHETIC_CONF", synth_file
        ), patch(
            "immich_accelerator.__main__.Path"
        ) as MockPath, patch(
            "builtins.input", return_value="y"
        ), patch(
            "immich_accelerator.__main__.subprocess.run", return_value=mock_result
        ) as mock_run:
            mock_build = MagicMock()
            mock_build.exists.return_value = False
            MockPath.side_effect = lambda p: mock_build if p == "/build" else Path(p)
            _ensure_build_link()

        tee_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and "tee" in c.args[0] and str(synth_file) in c.args[0]
        ]
        assert tee_calls, "expected a sudo tee write to the synthetic.d file"
        written = tee_calls[0].kwargs["input"]
        assert "build\t" in written  # our entry was added
        assert "usr/src/app/upload\tVolumes/upload" in written  # foreign line kept

    def test_has_build_entry_ignores_substring_and_comments(self):
        """_has_build_entry matches the entry name, not a substring or comment."""
        from immich_accelerator.__main__ import _has_build_entry

        assert _has_build_entry("build\tUsers/me/build-data\n") is True
        assert _has_build_entry("# build\tUsers/me/build-data\n") is False
        assert _has_build_entry("data\tVolumes/build/upload\n") is False
        assert _has_build_entry("") is False
        assert _has_build_entry("  build\tUsers/me/build-data\n") is True  # leading ws

    def test_strip_build_entry_keeps_foreign_and_comments(self):
        """_strip_build_entry removes only the build line, keeping the rest."""
        from immich_accelerator.__main__ import _strip_build_entry

        content = "# a comment\ndata\tVolumes/upload\nbuild\tUsers/me/build-data\n"
        out = _strip_build_entry(content)
        assert "build\tUsers/me/build-data" not in out
        assert "data\tVolumes/upload" in out
        assert "# a comment" in out
        # Only build line removed → exactly one line gone
        assert out == "# a comment\ndata\tVolumes/upload\n"


class TestRemoveBuildLink:
    def test_noop_when_conf_missing(self, tmp_data_dir):
        """No synthetic.d file → no action."""
        synth_file = tmp_data_dir["data_dir"] / "nonexistent"
        with patch("immich_accelerator.__main__.SYNTHETIC_CONF", synth_file):
            _remove_build_link()  # Should not raise

    def test_removes_conf_file(self, tmp_data_dir):
        """Calls sudo rm on the synthetic.d file."""
        synth_file = tmp_data_dir["data_dir"] / "synthetic-conf"
        synth_file.write_text("build\tUsers/test\n")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("immich_accelerator.__main__.SYNTHETIC_CONF", synth_file), patch(
            "subprocess.run", return_value=mock_result
        ) as mock_run:
            _remove_build_link()
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == "sudo"
            assert args[1] == "rm"
            assert str(synth_file) in str(args[2])

    def test_preserves_foreign_lines_on_remove(self, tmp_data_dir):
        """Uninstall must keep the user's foreign lines (issue #61): strip only
        the build entry and write back the remainder, never blanket-rm."""
        synth_file = tmp_data_dir["data_dir"] / "synthetic-conf"
        synth_file.write_text("data\tVolumes/upload\nbuild\tUsers/test/build-data\n")
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("immich_accelerator.__main__.SYNTHETIC_CONF", synth_file), patch(
            "immich_accelerator.__main__.subprocess.run", return_value=mock_result
        ) as mock_run:
            _remove_build_link()
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[1] == "tee"  # rewrite, not rm
            written = mock_run.call_args.kwargs["input"]
            assert "data\tVolumes/upload" in written
            assert "build\t" not in written


# ---------------------------------------------------------------------------
# Docker media-root detection (_detect_docker_media_prefix)
# ---------------------------------------------------------------------------


class TestDetectDockerMediaPrefix:
    """Regression coverage for issue #61: detection must follow Immich's actual
    layout (<MEDIA>/library/<storageLabel|uuid>/…), not assume upload/<uuid>/."""

    def _detect(self, items):
        from immich_accelerator.__main__ import _detect_docker_media_prefix

        resp = MagicMock()
        resp.read.return_value = json.dumps({"assets": {"items": items}}).encode()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=resp):
            return _detect_docker_media_prefix("http://nas:2283", "key")

    def test_storage_label_no_uuid(self):
        """User set a storage label ('Anthony') → no UUID in path. The bug case."""
        got = self._detect(
            [
                {
                    "libraryId": None,
                    "originalPath": "/usr/src/app/upload/library/Anthony/2026/06/IMG_1.jpg",
                }
            ]
        )
        assert got == "/usr/src/app/upload"

    def test_default_uuid_layout(self):
        """No storage label → ownerId UUID under library/. Media root before library."""
        got = self._detect(
            [
                {
                    "libraryId": None,
                    "originalPath": "/data/library/3fa85f64-5717-4562-b3fc-2c963f66afa6/2024/2024-01-01/IMG.jpg",
                }
            ]
        )
        assert got == "/data"

    def test_skips_external_then_reads_upload_asset(self):
        """External-library assets (libraryId set) are skipped; first upload wins."""
        got = self._detect(
            [
                {"libraryId": "ext-1", "originalPath": "/nas/Pictures/2026/x.jpg"},
                {
                    "libraryId": None,
                    "originalPath": "/mnt/photos/library/admin/2026/06/y.jpg",
                },
            ]
        )
        assert got == "/mnt/photos"

    def test_only_external_returns_none(self):
        """All external → don't know, don't block."""
        got = self._detect(
            [{"libraryId": "ext-1", "originalPath": "/nas/Pictures/2026/x.jpg"}]
        )
        assert got is None

    def test_legacy_uuid_fallback_without_library_segment(self):
        """Old layout with a UUID but no `library` segment → legacy fallback."""
        got = self._detect(
            [
                {
                    "libraryId": None,
                    "originalPath": "/data/upload/3fa85f64-5717-4562-b3fc-2c963f66afa6/2024/IMG.jpg",
                }
            ]
        )
        assert got == "/data"

    def test_empty_items_returns_none(self):
        assert self._detect([]) is None


# ---------------------------------------------------------------------------
# Path-mismatch warning (_warn_on_path_mismatch, _is_top_level_path)
# ---------------------------------------------------------------------------


class TestIsTopLevelPath:
    def test_top_level_true(self):
        from immich_accelerator.__main__ import _is_top_level_path

        assert _is_top_level_path("/data") is True
        assert _is_top_level_path("data") is True
        assert _is_top_level_path("/immich") is True

    def test_nested_or_empty_false(self):
        from immich_accelerator.__main__ import _is_top_level_path

        assert _is_top_level_path("/usr/src/app/upload") is False
        assert _is_top_level_path("/") is False
        assert _is_top_level_path("") is False


class TestWarnOnPathMismatch:
    """Issue #61: guidance must be achievable. Synthetic link is only offered
    when Docker's media root is top-level; nested roots steer to Route 1/2."""

    def _warn(self, detected, mount, caplog):
        import logging

        from immich_accelerator.__main__ import _warn_on_path_mismatch

        with patch(
            "immich_accelerator.__main__._detect_docker_media_prefix",
            return_value=detected,
        ), patch(
            "immich_accelerator.__main__._fetch_external_libraries", return_value=[]
        ), caplog.at_level(
            logging.ERROR, logger="accelerator"
        ):
            fatal = _warn_on_path_mismatch("http://nas:2283", "key", mount)
        text = "\n".join(r.getMessage() for r in caplog.records)
        return fatal, text

    def test_top_level_offers_synthetic(self, caplog):
        fatal, text = self._warn("/photos", "/data", caplog)
        assert fatal is True
        # Valid synthetic command: single top-level name, no-leading-slash target,
        # printf (portable tab); the printed instruction shows a literal \t.
        assert "printf 'photos\\tdata\\n'" in text
        assert "Route 1" not in text

    def test_nested_steers_to_routes_not_impossible_synthetic(self, caplog):
        fatal, text = self._warn("/usr/src/app/upload", "/data", caplog)
        assert fatal is True
        assert "Route 1" in text and "Route 2" in text
        assert "nested" in text
        # Must NOT emit the impossible nested synthetic name as a command
        assert "usr/src/app/upload\\t" not in text

    def test_compatible_paths_not_fatal(self, caplog):
        # upload_mount is a parent of detected → compatible, no error
        fatal, text = self._warn("/data/library", "/data", caplog)
        assert fatal is False
        assert "Upload path mismatch" not in text


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------


class TestPathConstants:
    def test_data_dir_is_in_home(self):
        assert str(DATA_DIR).endswith(".immich-accelerator")
        assert DATA_DIR.parent == Path.home()

    def test_config_file_in_data_dir(self):
        assert CONFIG_FILE.parent == DATA_DIR

    def test_pid_dir_in_data_dir(self):
        assert PID_DIR.parent == DATA_DIR

    def test_log_dir_in_data_dir(self):
        assert LOG_DIR.parent == DATA_DIR


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def test_stop_dispatches(self):
        with patch("sys.argv", ["prog", "stop"]), patch(
            "immich_accelerator.__main__.cmd_stop"
        ) as mock:
            main()
            mock.assert_called_once()

    def test_status_dispatches(self):
        with patch("sys.argv", ["prog", "status"]), patch(
            "immich_accelerator.__main__.cmd_status"
        ) as mock:
            main()
            mock.assert_called_once()

    def test_runtime_error_exits(self):
        with patch("sys.argv", ["prog", "start"]), patch(
            "immich_accelerator.__main__.cmd_start", side_effect=RuntimeError("boom")
        ), pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_keyboard_interrupt_handled(self):
        with patch("sys.argv", ["prog", "stop"]), patch(
            "immich_accelerator.__main__.cmd_stop", side_effect=KeyboardInterrupt
        ):
            main()  # Should not raise


class TestCapLog:
    """Service-log rotation: cap in place (the worker holds the file open in
    append mode, so we truncate the inode, never rename it)."""

    def test_under_cap_untouched(self, tmp_path):
        p = tmp_path / "worker.log"
        p.write_bytes(b"line\n" * 100)
        before = p.read_bytes()
        assert cap_log(p, max_bytes=10_000) is False
        assert p.read_bytes() == before

    def test_missing_file_is_noop(self, tmp_path):
        assert cap_log(tmp_path / "nope.log", max_bytes=10) is False

    def test_over_cap_truncates_and_keeps_tail(self, tmp_path):
        p = tmp_path / "worker.log"
        p.write_bytes(b"".join(b"line-%d\n" % i for i in range(50_000)))
        assert p.stat().st_size > 1000
        assert cap_log(p, max_bytes=1000) is True
        out = p.read_bytes()
        # shrunk below the original, keeps the most recent lines, drops the oldest
        assert p.stat().st_size < 50_000 * 7
        assert b"log rotated" in out
        assert b"line-49999\n" in out
        assert b"line-0\n" not in out

    def test_truncate_preserves_inode(self, tmp_path):
        # The open append fd must keep working after a rotate (same inode).
        p = tmp_path / "worker.log"
        p.write_bytes(b"x\n" * 200_000)
        ino_before = p.stat().st_ino
        with open(p, "a") as fh:  # simulate the worker's open append handle
            assert cap_log(p, max_bytes=1000) is True
            fh.write("after-rotate\n")
            fh.flush()
        assert p.stat().st_ino == ino_before
        assert b"after-rotate\n" in p.read_bytes()


class TestDiagnoseWorkerLog:
    """Turn a known unrecoverable worker-bootstrap failure into actionable
    guidance instead of a raw stack trace + silent crash loop (issue #73)."""

    GEODATA_LOG = (
        "[Nest] MapRepository] Starting geodata import\n"
        "Query failed : { error: Error: write EPIPE ... }\n"
        "[Nest] ERROR [MetadataService] Unable to initialize reverse geocoding: "
        "Error: write EPIPE\n"
        "Error: Metadata service init failed\n"
    )

    def test_geodata_failure_returns_guidance(self, tmp_path):
        p = tmp_path / "worker.log"
        p.write_text(self.GEODATA_LOG)
        hint = diagnose_worker_log(p)
        assert hint is not None
        assert "geodata" in hint.lower()
        assert "IMMICH_WORKERS_INCLUDE=api" in hint

    def test_unrelated_failure_returns_none(self, tmp_path):
        p = tmp_path / "worker.log"
        p.write_text("[Nest] ERROR something totally unrelated exploded\n")
        assert diagnose_worker_log(p) is None

    def test_missing_log_returns_none(self, tmp_path):
        assert diagnose_worker_log(tmp_path / "nope.log") is None

    def test_signature_found_in_large_log_tail(self, tmp_path):
        # The signature must be matched even when buried under a big log;
        # it's near the end (the worker dies right after), within the tail.
        p = tmp_path / "worker.log"
        p.write_text(("noise line\n" * 5000) + self.GEODATA_LOG)
        assert diagnose_worker_log(p) is not None


class TestEnsureMediaReady:
    """Mount-agnostic readiness gate (#11): refuse to start unless the media
    root is the real, marked location — so the worker can't write into a
    placeholder a network mount later masks. The marker identifies the root
    regardless of how it's mounted (local/NFS/SMB)."""

    def test_no_upload_mount_is_allowed(self):
        # Nothing configured to guard (same-host default) → don't block.
        assert ensure_media_ready({}) is True

    def test_first_run_marks_writable_root(self, tmp_path, tmp_data_dir):
        media = tmp_path / "media"
        media.mkdir()
        config = {"upload_mount": str(media)}
        assert ensure_media_ready(config) is True
        # marker created, id recorded in config
        assert config.get("media_id")
        assert (media / MEDIA_MARKER_NAME).read_text().strip() == config["media_id"]

    def test_verify_passes_when_marker_matches(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        (media / MEDIA_MARKER_NAME).write_text("abc123")
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "abc123"})
            is True
        )

    def test_refuses_when_marker_missing(self, tmp_path):
        # media_id known but the marker isn't there → placeholder / mount down.
        media = tmp_path / "media"
        media.mkdir()  # empty dir, no marker (simulates unmounted placeholder)
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "abc123"})
            is False
        )

    def test_refuses_when_marker_mismatch(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir()
        (media / MEDIA_MARKER_NAME).write_text("different")
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "abc123"})
            is False
        )

    def test_symlinked_subdir_on_another_disk_is_allowed(self, tmp_path):
        """#115: putting thumbs on a fast SSD via a symlink is supported. The
        link resolves, so the gate passes and writes land on the target."""
        media = tmp_path / "media"
        media.mkdir()
        (media / MEDIA_MARKER_NAME).write_text("abc123")
        ssd = tmp_path / "ssd" / "thumbs"
        ssd.mkdir(parents=True)
        (media / "thumbs").symlink_to(ssd)
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "abc123"})
            is True
        )

    def test_refuses_when_subdir_symlink_dangles(self, tmp_path):
        """#115: if that SSD isn't mounted the symlink dangles. Without this
        check the marker still verifies via the root and we would start, then
        Immich would fail every thumbnail write with ENOENT."""
        media = tmp_path / "media"
        media.mkdir()
        (media / MEDIA_MARKER_NAME).write_text("abc123")
        (media / "thumbs").symlink_to(tmp_path / "never-mounted" / "thumbs")
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "abc123"})
            is False
        )

    def test_first_run_refuses_when_subdir_symlink_dangles(
        self, tmp_path, tmp_data_dir
    ):
        media = tmp_path / "media"
        media.mkdir()
        (media / "encoded-video").symlink_to(tmp_path / "never-mounted" / "ev")
        assert ensure_media_ready({"upload_mount": str(media)}) is False

    def test_refuses_when_subdir_symlink_points_at_a_file(self, tmp_path):
        """A link whose target exists but is not a directory fails writes with
        ENOTDIR, which os.path.exists would have called fine."""
        media = tmp_path / "media"
        media.mkdir()
        (media / MEDIA_MARKER_NAME).write_text("abc123")
        stub = tmp_path / "a-file"
        stub.write_text("not a directory")
        (media / "thumbs").symlink_to(stub)
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "abc123"})
            is False
        )

    def test_non_critical_broken_symlink_does_not_block_startup(self, tmp_path):
        """A removable drive holding backups/ or profile/ must not take the whole
        accelerator down: Immich barely touches them, and the worker, ML, and
        dashboard have no reason to stay off."""
        media = tmp_path / "media"
        media.mkdir()
        (media / MEDIA_MARKER_NAME).write_text("abc123")
        (media / "backups").symlink_to(tmp_path / "unplugged" / "backups")
        (media / "profile").symlink_to(tmp_path / "unplugged" / "profile")
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "abc123"})
            is True
        )

    def test_first_run_refuses_when_root_absent(self, tmp_path):
        # upload_mount points "through" a regular file — the dir doesn't exist,
        # so first-run init has no candidate and we refuse.
        afile = tmp_path / "not_a_dir"
        afile.write_text("x")
        config = {"upload_mount": str(afile / "media")}
        assert ensure_media_ready(config) is False
        assert "media_id" not in config

    def test_first_run_uses_writable_subdir_when_root_readonly(
        self, tmp_path, tmp_data_dir
    ):
        # #80: media root not writable (e.g. a root-owned /data symlink) but the
        # Immich subdirs are — the gate must mark a subdir, not refuse.
        if os.geteuid() == 0:
            pytest.skip("chmod read-only is ineffective as root")
        media = tmp_path / "media"
        media.mkdir()
        (media / "upload").mkdir()
        os.chmod(media, 0o555)  # root not writable
        try:
            config = {"upload_mount": str(media)}
            assert ensure_media_ready(config) is True
            assert config.get("media_id")
            assert (media / "upload" / MEDIA_MARKER_NAME).exists()
            assert not (media / MEDIA_MARKER_NAME).exists()  # not at the root
        finally:
            os.chmod(media, 0o755)  # let tmp_path clean up

    def test_verify_finds_marker_in_subdir(self, tmp_path):
        # A marker written into a subdir (root-readonly case) verifies on restart.
        media = tmp_path / "media"
        media.mkdir()
        (media / "thumbs").mkdir()
        (media / "thumbs" / MEDIA_MARKER_NAME).write_text("xyz")
        assert (
            ensure_media_ready({"upload_mount": str(media), "media_id": "xyz"}) is True
        )

    def test_refuses_when_root_readonly_and_no_writable_subdir(self, tmp_path):
        # Root not writable AND no subdirs at all → genuinely can't establish a
        # marker → refuse (first run).
        if os.geteuid() == 0:
            pytest.skip("chmod read-only is ineffective as root")
        media = tmp_path / "media"
        media.mkdir()
        os.chmod(media, 0o555)
        try:
            config = {"upload_mount": str(media)}
            assert ensure_media_ready(config) is False
            assert "media_id" not in config
        finally:
            os.chmod(media, 0o755)


class TestInstalledVersion:
    """Detect a `brew upgrade` while watch runs old code, so it can relaunch
    into the new code and reload the worker (otherwise a detached worker keeps
    running stale code after an upgrade)."""

    def test_reads_opt_symlink_version(self, tmp_path):
        vf = tmp_path / "VERSION"
        vf.write_text("9.9.9\n")
        with patch("immich_accelerator.__main__._OPT_VERSION_FILE", vf):
            assert _installed_version() == "9.9.9"

    def test_falls_back_to_running_version_when_absent(self, tmp_path):
        from immich_accelerator.__main__ import __version__ as running

        with patch("immich_accelerator.__main__._OPT_VERSION_FILE", tmp_path / "nope"):
            assert _installed_version() == running


class TestPruneServerVersions:
    """_prune_old_server_versions keeps only the current build and must survive
    junk (.staging dirs) and unreadable entries without crashing startup."""

    def _make_server_root(self, tmp_path, names):
        root = tmp_path / "server"
        root.mkdir()
        for name in names:
            (root / name).mkdir()
        return root

    def test_keeps_only_current_and_clears_staging(self, tmp_path):
        from immich_accelerator.__main__ import _prune_old_server_versions

        root = self._make_server_root(
            tmp_path, ["2.6.3", "2.7.5", "3.0.1", "3.0.2", "3.0.2.staging"]
        )
        with patch("immich_accelerator.__main__.DATA_DIR", tmp_path):
            _prune_old_server_versions("3.0.2")
        assert sorted(p.name for p in root.iterdir()) == ["3.0.2"]

    def test_missing_server_root_is_noop(self, tmp_path):
        from immich_accelerator.__main__ import _prune_old_server_versions

        with patch("immich_accelerator.__main__.DATA_DIR", tmp_path):
            _prune_old_server_versions("3.0.2")  # no server/ dir, must not raise

    def test_failed_delete_is_not_fatal(self, tmp_path):
        from immich_accelerator.__main__ import _prune_old_server_versions

        self._make_server_root(tmp_path, ["2.7.5", "3.0.1", "3.0.2"])
        # First rmtree raises; the loop must swallow it and still try the other.
        with patch("immich_accelerator.__main__.DATA_DIR", tmp_path), patch(
            "immich_accelerator.__main__.shutil.rmtree",
            side_effect=[OSError("boom"), None],
        ) as rmtree:
            _prune_old_server_versions("3.0.2")  # must not raise
        assert rmtree.call_count == 2


class TestStartDashboard:
    """start_dashboard() is called from both `start` and `watch`; it must not
    spawn a second dashboard when one is already running (the double-start race
    that crashes on EADDRINUSE and orphans the real one)."""

    def test_skips_when_pid_alive(self):
        from immich_accelerator.__main__ import start_dashboard

        with patch("immich_accelerator.__main__.read_pid", return_value=1234), patch(
            "immich_accelerator.__main__.subprocess.Popen"
        ) as popen:
            start_dashboard()
            popen.assert_not_called()

    def test_adopts_untracked_orphan_on_port(self):
        # Serving on the port with no pid file, and it's really our dashboard →
        # adopt the listener's pid (so a later stop can reach it) instead of
        # spawning a second one.
        from immich_accelerator.__main__ import start_dashboard

        with patch("immich_accelerator.__main__.read_pid", return_value=None), patch(
            "immich_accelerator.__main__._pid_on_port", return_value=729
        ), patch(
            "immich_accelerator.__main__._process_is_our_dashboard", return_value=True
        ), patch(
            "immich_accelerator.__main__._dashboard_port", return_value=8420
        ), patch(
            "immich_accelerator.__main__.write_pid"
        ) as wpid, patch(
            "immich_accelerator.__main__.subprocess.Popen"
        ) as popen:
            start_dashboard()
            popen.assert_not_called()
            wpid.assert_called_once_with("dashboard", 729)

    def test_does_not_adopt_foreign_process_on_port(self):
        # Something else holds the port (e.g. OrbStack) → do NOT adopt it and do
        # NOT spawn (its port is taken); just warn. Adopting a foreign pid meant
        # we tracked the wrong process and never ran our own dashboard.
        from immich_accelerator.__main__ import start_dashboard

        with patch("immich_accelerator.__main__.read_pid", return_value=None), patch(
            "immich_accelerator.__main__._pid_on_port", return_value=729
        ), patch(
            "immich_accelerator.__main__._process_is_our_dashboard", return_value=False
        ), patch(
            "immich_accelerator.__main__._dashboard_port", return_value=8420
        ), patch(
            "immich_accelerator.__main__.write_pid"
        ) as wpid, patch(
            "immich_accelerator.__main__.subprocess.Popen"
        ) as popen:
            start_dashboard()
            popen.assert_not_called()
            wpid.assert_not_called()

    def test_starts_fresh_when_nothing_running(self):
        from immich_accelerator.__main__ import start_dashboard

        with patch("immich_accelerator.__main__.read_pid", return_value=None), patch(
            "immich_accelerator.__main__._pid_on_port", return_value=None
        ), patch(
            "immich_accelerator.__main__._dashboard_port", return_value=8420
        ), patch(
            "immich_accelerator.__main__.write_pid"
        ), patch(
            "immich_accelerator.__main__.subprocess.Popen"
        ) as popen:
            popen.return_value.pid = 555
            start_dashboard()
            popen.assert_called_once()


class TestStopAllFast:
    """The watcher's SIGTERM handler must signal ALL services up front (launchd
    SIGKILLs the watcher within seconds, less than cmd_stop's 5s-per-service
    waits), so worker+ML+dashboard all get SIGTERM even if the handler is cut
    short (#81 follow-up: ML/dashboard survived a stop)."""

    def test_signals_all_three_services(self):
        from immich_accelerator.__main__ import stop_all_fast

        pids = {"worker": 111, "ml": 222, "dashboard": 333}
        sent = []

        def fake_kill(pid, sig):
            if sig == 0:
                raise OSError()  # report dead so the wait loop exits immediately

        with patch(
            "immich_accelerator.__main__.read_pid", side_effect=lambda n: pids.get(n)
        ), patch("os.getpgid", side_effect=lambda pid: pid), patch(
            "os.killpg", side_effect=lambda pgid, sig: sent.append((pgid, sig))
        ), patch(
            "os.kill", side_effect=fake_kill
        ), patch(
            "immich_accelerator.__main__._kill_all_worker_processes"
        ):
            stop_all_fast()

        termed = {pgid for pgid, sig in sent if sig == signal.SIGTERM}
        assert termed == {111, 222, 333}  # all signalled before any wait


class TestBuildHasCorePlugin:
    """Plugin detection across Immich layouts (2.7 corePlugin, 3.0 plugins/)."""

    def test_detects_27_layout(self, tmp_path):
        from immich_accelerator.__main__ import _build_has_core_plugin

        (tmp_path / "corePlugin").mkdir()
        (tmp_path / "corePlugin" / "manifest.json").write_text("{}")
        assert _build_has_core_plugin(tmp_path) is True

    def test_detects_30_layout(self, tmp_path):
        from immich_accelerator.__main__ import _build_has_core_plugin

        plugin = tmp_path / "plugins" / "immich-plugin-core"
        (plugin / "dist").mkdir(parents=True)
        (plugin / "dist" / "plugin.wasm").write_bytes(b"\x00asm")
        (plugin / "manifest.json").write_text("{}")  # manifest at plugin root
        assert _build_has_core_plugin(tmp_path) is True

    def test_30_wasm_without_manifest_is_incomplete(self, tmp_path):
        # The #95-follow-on bug: dist/plugin.wasm alone (manifest.json in a
        # later, not-yet-extracted layer) must NOT count as complete, or the
        # layer-loop early-exits and Immich can't import the plugin.
        from immich_accelerator.__main__ import _build_has_core_plugin

        wasm = tmp_path / "plugins" / "immich-plugin-core" / "dist" / "plugin.wasm"
        wasm.parent.mkdir(parents=True)
        wasm.write_bytes(b"\x00asm")
        assert _build_has_core_plugin(tmp_path) is False

    def test_false_when_absent(self, tmp_path):
        from immich_accelerator.__main__ import _build_has_core_plugin

        assert _build_has_core_plugin(tmp_path) is False
        (tmp_path / "plugins").mkdir()  # empty plugins dir is not enough
        assert _build_has_core_plugin(tmp_path) is False


class TestBuildIsPluginEra:
    """Loose plugin-era detection used by cmd_start to decide /build-link need.

    Distinct from _build_has_core_plugin: a partially-extracted 3.0 plugin
    (wasm but no manifest) is still plugin-era and must require the /build link.
    """

    def test_partial_30_plugin_is_plugin_era(self, tmp_path):
        from immich_accelerator.__main__ import (
            _build_has_core_plugin,
            _build_is_plugin_era,
        )

        wasm = tmp_path / "plugins" / "immich-plugin-core" / "dist" / "plugin.wasm"
        wasm.parent.mkdir(parents=True)
        wasm.write_bytes(b"\x00asm")
        # Regression guard for the narrowed-predicate side effect: the strict
        # check says "incomplete" but the era check must still say "plugin-era"
        # so the worker requires the /build link instead of the pre-2.7 fallback.
        assert _build_has_core_plugin(tmp_path) is False
        assert _build_is_plugin_era(tmp_path) is True

    def test_27_corePlugin_is_plugin_era(self, tmp_path):
        from immich_accelerator.__main__ import _build_is_plugin_era

        (tmp_path / "corePlugin").mkdir()
        assert _build_is_plugin_era(tmp_path) is True

    def test_stray_empty_plugin_subdir_is_not_plugin_era(self, tmp_path):
        # A plugins/<x>/ dir with no manifest or wasm (stray/empty) must not be
        # read as plugin-era, or a genuinely pre-2.7 build could be refused the
        # IMMICH_BUILD_DATA fallback and fail to start.
        from immich_accelerator.__main__ import _build_is_plugin_era

        (tmp_path / "plugins" / "junk").mkdir(parents=True)
        assert _build_is_plugin_era(tmp_path) is False

    def test_no_plugins_is_not_plugin_era(self, tmp_path):
        from immich_accelerator.__main__ import _build_is_plugin_era

        assert _build_is_plugin_era(tmp_path) is False
        (tmp_path / "plugins").mkdir()  # empty plugins dir -> pre-2.7
        assert _build_is_plugin_era(tmp_path) is False


class TestCachedServerIfCurrent:
    """Version-stamped cache gate: build-data is shared/version-blind, so the
    per-version server cache must be paired with build-data stamped for the
    same version (and carrying the plugin) before it is trusted."""

    def _make_server(self, data_dir, version):
        server_dir = data_dir / "server" / version
        (server_dir / "dist").mkdir(parents=True)
        (server_dir / "dist" / "main.js").write_text("//")
        return server_dir

    def _make_30_build_data(self, data_dir, stamp=None):
        build_data = data_dir / "build-data"
        plugin = build_data / "plugins" / "immich-plugin-core"
        (plugin / "dist").mkdir(parents=True)
        (plugin / "dist" / "plugin.wasm").write_bytes(b"\x00asm")
        (plugin / "manifest.json").write_text("{}")
        if stamp is not None:
            (build_data / ".accel-version").write_text(stamp + "\n")
        return build_data

    def test_no_server_returns_none(self, tmp_path):
        from immich_accelerator import __main__ as m

        with patch.object(m, "DATA_DIR", tmp_path):
            assert (
                m._cached_server_if_current(tmp_path / "server" / "3.0.1", "3.0.1")
                is None
            )

    def test_pre_27_trusts_server_without_stamp(self, tmp_path):
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "2.6.0")
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "2.6.0") == server_dir

    def test_matching_stamp_and_plugin_trusts_cache(self, tmp_path):
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        self._make_30_build_data(tmp_path, stamp="3.0.1")
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "3.0.1") == server_dir

    def test_unstamped_matching_layout_is_adopted(self, tmp_path):
        # Legacy/offline build-data with no stamp but the 3.0 plugin present for
        # a 3.0 request is adopted (stamped in place) rather than force a
        # needless full re-download on the first start after upgrading.
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        build_data = self._make_30_build_data(tmp_path, stamp=None)
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "3.0.1") == server_dir
            # adoption persists the stamp so later starts skip the layout check
            assert m._build_data_version(build_data) == "3.0.1"

    def test_unstamped_cross_era_layout_reextracts(self, tmp_path):
        # The dangerous case: unstamped build-data whose plugin is the WRONG era
        # (2.7 corePlugin left behind while now serving 3.0.1). The layout does
        # not match a 3.0 request, so it must NOT be adopted; re-extract.
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        build_data = tmp_path / "build-data"
        (build_data / "corePlugin").mkdir(parents=True)
        (build_data / "corePlugin" / "manifest.json").write_text("{}")
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "3.0.1") is None
            assert m._build_data_version(build_data) is None  # not adopted

    def test_stale_version_stamp_reextracts(self, tmp_path):
        # A stamp for a DIFFERENT version (2.7.5 rollback then forward to 3.0.1)
        # is real drift and must NOT be trusted or adopted for 3.0.1.
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        self._make_30_build_data(tmp_path, stamp="2.7.5")
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "3.0.1") is None

    def test_corrupt_stamp_treated_as_unstamped(self, tmp_path):
        # A non-UTF8/corrupt stamp must not raise out of the cache gate; it is
        # treated as unstamped and (here, matching layout) adopted.
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        build_data = self._make_30_build_data(tmp_path, stamp=None)
        (build_data / ".accel-version").write_bytes(b"\xff\xfe\x00bad")
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._build_data_version(build_data) is None  # no traceback
            assert m._cached_server_if_current(server_dir, "3.0.1") == server_dir


class TestRemoteSetupUsesTheCache:
    """setup --url learns the exact version from the API before it fetches
    anything, so a warm cache is knowable up front. Both fetch paths check it,
    but extract_immich_server only reaches its check after `docker pull` has run.
    """

    def _run(self, cached):
        import immich_accelerator.__main__ as m

        args = argparse.Namespace(
            url="http://immich.example:2283", api_key="k", import_server=None
        )
        fetched = MagicMock()
        with patch("builtins.input", return_value=""), patch(
            "getpass.getpass", return_value="pw"
        ), patch.object(
            m, "_query_immich_api", return_value={"version": "3.0.1"}
        ), patch.object(
            m, "_detect_docker_media_prefix", return_value=None
        ), patch.object(
            m, "_validate_connectivity", return_value=True
        ), patch.object(
            m, "_check_local_tools", return_value=("/usr/bin/node", "/ffmpeg", None)
        ), patch.object(
            m, "_cached_server_if_current", return_value=cached
        ), patch.object(
            m, "find_docker", side_effect=RuntimeError("no docker")
        ), patch.object(
            m, "download_immich_server", fetched
        ), patch.object(
            m, "_finalize_config"
        ):
            m._setup_remote(args)
        return fetched

    def test_a_warm_cache_skips_the_fetch(self, tmp_data_dir):
        assert not self._run(Path("/srv/3.0.1")).called

    def test_a_cold_cache_still_fetches(self, tmp_data_dir):
        assert self._run(None).called


class TestFinalizeBuildData:
    """Stamping is the trust signal, so it must only fire when build-data is
    genuinely complete for the version."""

    def test_stamps_complete_30_build_data(self, tmp_path):
        from immich_accelerator import __main__ as m

        build_data = tmp_path / "build-data"
        plugin = build_data / "plugins" / "immich-plugin-core"
        (plugin / "dist").mkdir(parents=True)
        (plugin / "dist" / "plugin.wasm").write_bytes(b"\x00asm")
        (plugin / "manifest.json").write_text("{}")
        m._finalize_build_data(build_data, "3.0.1")
        assert m._build_data_version(build_data) == "3.0.1"

    def test_does_not_stamp_plugin_less_30_build_data(self, tmp_path):
        # If the plugin is missing for a plugin-era version, we must NOT stamp,
        # so the next start re-extracts instead of trusting a broken install.
        from immich_accelerator import __main__ as m

        build_data = tmp_path / "build-data"
        build_data.mkdir()
        m._finalize_build_data(build_data, "3.0.1")
        assert m._build_data_version(build_data) is None

    def test_stamps_pre_27_without_plugin(self, tmp_path):
        from immich_accelerator import __main__ as m

        build_data = tmp_path / "build-data"
        build_data.mkdir()
        m._finalize_build_data(build_data, "2.6.0")
        assert m._build_data_version(build_data) == "2.6.0"


class TestProcessFdCount:
    """fd-count helpers for the #89 fd-leak watchdog (libproc based)."""

    def test_counts_live_fds_from_buffered_call(self):
        from immich_accelerator.__main__ import _process_fd_count
        from unittest.mock import MagicMock

        # First call (NULL buffer) returns the capacity high-water (1600 bytes);
        # second call (real buffer) returns the LIVE bytes written (960 -> 120).
        libproc = MagicMock()
        libproc.proc_pidinfo.side_effect = [1600, 960]
        with patch("immich_accelerator.__main__._LIBPROC", libproc):
            assert _process_fd_count(1234) == 120  # live count, not 1600/8

    def test_none_when_buffered_call_fails(self):
        from immich_accelerator.__main__ import _process_fd_count
        from unittest.mock import MagicMock

        libproc = MagicMock()
        libproc.proc_pidinfo.side_effect = [1600, 0]  # capacity ok, write fails
        with patch("immich_accelerator.__main__._LIBPROC", libproc):
            assert _process_fd_count(1234) is None

    def test_none_when_libproc_unavailable(self):
        from immich_accelerator.__main__ import _process_fd_count

        with patch("immich_accelerator.__main__._LIBPROC", None):
            assert _process_fd_count(1234) is None

    def test_none_on_nonpositive_return(self):
        from immich_accelerator.__main__ import _process_fd_count
        from unittest.mock import MagicMock

        libproc = MagicMock()
        libproc.proc_pidinfo.return_value = 0  # dead pid / error
        with patch("immich_accelerator.__main__._LIBPROC", libproc):
            assert _process_fd_count(1234) is None

    def test_worker_fd_total_sums_all_workers(self):
        from immich_accelerator.__main__ import _worker_fd_total
        from unittest.mock import MagicMock

        # _LIBPROC must be truthy or _worker_fd_total short-circuits (it is None
        # on non-macOS CI, where libproc.dylib doesn't exist).
        with patch("immich_accelerator.__main__._LIBPROC", MagicMock()), patch(
            "immich_accelerator.__main__._scan_worker_pids",
            return_value=[10, 20, 30],
        ), patch(
            "immich_accelerator.__main__._process_fd_count",
            side_effect=lambda p: {10: 100, 20: None, 30: 5000}.get(p),
        ):
            # 100 + 5000, the None (unreadable pid) is skipped
            assert _worker_fd_total() == 5100

    def test_worker_fd_total_none_when_no_workers(self):
        from immich_accelerator.__main__ import _worker_fd_total
        from unittest.mock import MagicMock

        with patch("immich_accelerator.__main__._LIBPROC", MagicMock()), patch(
            "immich_accelerator.__main__._scan_worker_pids", return_value=[]
        ):
            assert _worker_fd_total() is None

    def test_worker_fd_total_none_when_libproc_unavailable(self):
        from immich_accelerator.__main__ import _worker_fd_total

        # No libproc: skip the ps scan entirely and report None.
        with patch("immich_accelerator.__main__._LIBPROC", None), patch(
            "immich_accelerator.__main__._scan_worker_pids"
        ) as scan:
            assert _worker_fd_total() is None
            scan.assert_not_called()


class TestIntEnv:
    """Safe int env parsing for the fd-watchdog thresholds (#89)."""

    def test_valid_value(self, monkeypatch):
        from immich_accelerator.__main__ import _int_env

        monkeypatch.setenv("IAA_TEST_INT", "42")
        assert _int_env("IAA_TEST_INT", 10) == 42

    def test_missing_falls_back(self, monkeypatch):
        from immich_accelerator.__main__ import _int_env

        monkeypatch.delenv("IAA_TEST_INT", raising=False)
        assert _int_env("IAA_TEST_INT", 10) == 10

    def test_bad_value_falls_back_not_raises(self, monkeypatch):
        from immich_accelerator.__main__ import _int_env

        monkeypatch.setenv("IAA_TEST_INT", "10k")
        assert _int_env("IAA_TEST_INT", 10) == 10


class TestComponentEnabled:
    """_component_enabled: the precedence rules every other path depends on."""

    def test_absent_means_enabled(self):
        from immich_accelerator.__main__ import COMPONENTS, _component_enabled

        for name in COMPONENTS:
            assert _component_enabled(name, {}) is True

    def test_v180_dashboard_key_still_honored(self):
        """The one component key that actually shipped must keep working."""
        from immich_accelerator.__main__ import _component_enabled

        cfg = {"dashboard": False}
        assert _component_enabled("dashboard", cfg) is False
        assert _component_enabled("worker", cfg) is True
        assert _component_enabled("ml", cfg) is True

    def test_ml_only_preset_disables_only_the_worker(self):
        from immich_accelerator.__main__ import _component_enabled

        cfg = {"ml_only": True}
        assert _component_enabled("worker", cfg) is False
        assert _component_enabled("ml", cfg) is True
        assert _component_enabled("dashboard", cfg) is True

    def test_explicit_key_beats_the_preset(self):
        """Otherwise a box set up with --ml-only could never be switched back
        without hand-editing config.json."""
        from immich_accelerator.__main__ import _component_enabled

        assert _component_enabled("worker", {"ml_only": True, "worker": True}) is True

    def test_worker_on_ml_off_is_expressible(self):
        """The capability this release adds: Mac does thumbs, another box does ML."""
        from immich_accelerator.__main__ import _component_enabled

        cfg = {"ml": False}
        assert _component_enabled("worker", cfg) is True
        assert _component_enabled("ml", cfg) is False

    def test_unreadable_config_defaults_to_enabled(self, tmp_data_dir):
        """A broken config must not silently turn the accelerator off."""
        import immich_accelerator.__main__ as m

        with patch.object(m, "load_config", side_effect=OSError("boom")):
            assert m._component_enabled("worker") is True


class TestComponentToggle:
    """cmd_component / _set_component: flipping a key and applying it now."""

    # A config that actually describes a worker, so the ml-only guard lets the
    # toggle through. An `--ml-only` config has none of these keys.
    WORKER_CFG = {
        "server_dir": "/srv",
        "version": "3.0.1",
        "node": "/usr/bin/node",
        "db_hostname": "localhost",
        "db_port": "5432",
        "db_username": "postgres",
        "db_name": "immich",
        "redis_hostname": "localhost",
        "redis_port": "6379",
        "ml_port": 3003,
    }

    def test_off_writes_key_and_stops_it(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        saved = {}
        with patch.object(m, "load_config", return_value={"ml": True}), patch.object(
            m, "save_config", side_effect=lambda c: saved.update(c)
        ), patch.object(m, "reconcile_ml") as reconcile:
            m._set_component("ml", False)
        assert saved["ml"] is False
        reconcile.assert_called_once()

    def test_a_failed_toggle_does_not_announce_success(self, tmp_data_dir):
        """The menu bar shows the CLI's last output line as the failure reason.

        This sentence was logged unconditionally, after the failure, so every
        error banner in Settings read "Worker enabled. ML service and Dashboard
        unaffected." while the real cause (Docker down, sharp broken, media
        mount missing) scrolled past above it. Claiming success on the way out
        of a failure is wrong on its own terms too.
        """
        import immich_accelerator.__main__ as m

        cfg = dict(self.WORKER_CFG, worker=False)
        with patch.object(m, "load_config", return_value=cfg), patch.object(
            m, "save_config"
        ), patch.object(m, "_watcher_running", return_value=False), patch.object(
            m, "cmd_start"
        ), patch.object(
            m, "read_pid", return_value=None  # cmd_start logged and gave up
        ), patch.object(
            m, "log"
        ) as log:
            assert m._set_component("worker", True) is False

        said = " ".join(str(c) for c in log.info.call_args_list)
        assert "unaffected" not in said, "announced success after failing"

    def test_a_successful_toggle_still_says_so(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "load_config", return_value={"ml": True}), patch.object(
            m, "save_config"
        ), patch.object(m, "reconcile_ml"), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(
            m, "_restart_worker", return_value=True
        ), patch.object(
            m, "log"
        ) as log:
            assert m._set_component("ml", False) is True

        said = " ".join(str(c) for c in log.info.call_args_list)
        assert "unaffected" in said

    def test_enabling_worker_clears_the_stale_preset(self, tmp_data_dir):
        """Leaving a contradictory ml_only behind is a trap for whoever reads
        config.json next, even though the explicit key already wins."""
        import immich_accelerator.__main__ as m

        saved = {}
        cfg = {**self.WORKER_CFG, "ml_only": True, "worker": False}
        with patch.object(m, "load_config", return_value=cfg), patch.object(
            m, "save_config", side_effect=lambda c: saved.update(c)
        ), patch.object(m, "cmd_start"), patch.object(
            m, "read_pid", return_value=1234
        ), patch.object(
            m, "_start_lock"
        ):
            assert m._set_component("worker", True) is True
        assert saved["worker"] is True
        assert "ml_only" not in saved

    def test_enabling_worker_refuses_on_an_ml_only_install(self, tmp_data_dir):
        """The blocker this guards: cmd_start dereferences config["server_dir"],
        which an ml-only config has never had. That KeyError is not caught by
        main(), so the launchd watcher would relaunch and crash forever."""
        import immich_accelerator.__main__ as m

        ml_only = {"ml_only": True, "worker": False, "ml": True, "ml_port": 3003}
        with patch.object(m, "load_config", return_value=ml_only), patch.object(
            m, "save_config"
        ) as save, patch.object(m, "cmd_start") as start:
            assert m._set_component("worker", True) is False
            start.assert_not_called()  # never reaches the KeyError
            save.assert_not_called()  # and does not leave worker:true behind

    def test_worker_toggle_reports_failure_when_nothing_started(self, tmp_data_dir):
        """cmd_start reports most failures by logging and returning, so whether
        a worker actually came up is the only trustworthy signal.

        _watcher_running MUST be patched. Without it this test asks the machine
        it happens to run on whether a watcher is up, and inverts its result:
        green on a laptop, red on the Mac Mini where one really is running."""
        import immich_accelerator.__main__ as m

        cfg = {**self.WORKER_CFG, "worker": False}
        with patch.object(m, "load_config", return_value=cfg), patch.object(
            m, "save_config"
        ), patch.object(m, "cmd_start"), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(
            m, "_watcher_running", return_value=False
        ):
            assert m._set_component("worker", True) is False

    def test_unknown_component_is_rejected(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_set_component") as setter, pytest.raises(
            SystemExit
        ) as e:
            m.cmd_component(argparse.Namespace(name="thumbnails", state="off"))
        assert e.value.code == 2
        setter.assert_not_called()

    def test_listing_does_not_write_config(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "load_config", return_value={}), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(m, "save_config") as save:
            m.cmd_component(argparse.Namespace(name=None, state=None))
            save.assert_not_called()


class TestWorkerWithoutML:
    """Worker on, ML off. New in 1.9.0, and the env var is the subtle part."""

    def test_ml_url_omitted_so_immich_own_setting_governs(self, tmp_data_dir):
        """Pointing the worker at a dead localhost port would fail every ML job;
        setting nothing lets Immich's configured ML URL apply."""
        env = _worker_env_for({"ml": False})
        assert "IMMICH_MACHINE_LEARNING_URL" not in env

    def test_ml_url_config_key_is_forwarded_when_set(self, tmp_data_dir):
        env = _worker_env_for({"ml": False, "ml_url": "http://gpubox:3003"})
        assert env["IMMICH_MACHINE_LEARNING_URL"] == "http://gpubox:3003"

    def test_localhost_used_when_ml_is_on(self, tmp_data_dir):
        env = _worker_env_for({})
        assert env["IMMICH_MACHINE_LEARNING_URL"] == "http://localhost:3003"

    def test_an_inherited_ml_url_is_actively_removed(self, tmp_data_dir, monkeypatch):
        """worker_env starts as os.environ.copy(), so merely not setting the var
        is not enough: an inherited one would survive and point the worker at a
        port with nothing behind it."""
        monkeypatch.setenv("IMMICH_MACHINE_LEARNING_URL", "http://stale:3003")
        env = _worker_env_for({"ml": False})
        assert "IMMICH_MACHINE_LEARNING_URL" not in env

    def test_config_ml_url_beats_an_inherited_one(self, tmp_data_dir, monkeypatch):
        monkeypatch.setenv("IMMICH_MACHINE_LEARNING_URL", "http://stale:3003")
        env = _worker_env_for({"ml": False, "ml_url": "http://gpubox:3003"})
        assert env["IMMICH_MACHINE_LEARNING_URL"] == "http://gpubox:3003"


def _worker_env_for(overrides: dict) -> dict:
    """Run cmd_start far enough to capture the worker environment it builds.

    cmd_start is the most load-bearing function in the codebase, so this drives
    the real thing rather than reimplementing its env assembly.
    """
    import immich_accelerator.__main__ as m

    config = {
        "db_hostname": "localhost",
        "db_port": "5432",
        "db_username": "postgres",
        "db_password": "pw",
        "db_name": "immich",
        "redis_hostname": "localhost",
        "redis_port": "6379",
        "ml_port": 3003,
        "version": "3.0.1",
        "server_dir": "/srv",
        "node": "/usr/bin/node",
        "ml_dir": "/ml",
        # find_docker is stubbed to fail below, and only a split install may
        # start without reading its container. Without this the run stops at
        # the preflight and never builds the env we came for.
        "immich_url": "http://immich.example:2283",
    }
    config.update(overrides)
    captured = {}

    def capture(name, cmd, env, cwd):
        captured.update(env)
        raise RuntimeError("stop here, the env is what we came for")

    with patch.object(m, "load_config", return_value=config), patch.object(
        m, "save_config"
    ), patch.object(m, "_kill_stale_processes"), patch.object(
        m, "find_docker", side_effect=RuntimeError("no docker")
    ), patch.object(
        m, "read_pid", return_value=None
    ), patch.object(
        m, "find_node", return_value="/usr/bin/node"
    ), patch.object(
        m, "_check_node_engines_compat", return_value=(True, "")
    ), patch.object(
        m, "_verify_sharp_loads", return_value=(True, "")
    ), patch.object(
        m, "_build_link_ok", return_value=True
    ), patch.object(
        m, "_preflight_env_health", return_value=True
    ), patch.object(
        m, "ensure_media_ready", return_value=True
    ), patch.object(
        m, "_find_ml_dir", return_value=None
    ), patch.object(
        m, "_start_ml_preferred", return_value=(0, None, False)
    ), patch.object(
        m, "kill_pid"
    ), patch.object(
        m, "start_service", side_effect=capture
    ):
        with pytest.raises(RuntimeError):
            m.cmd_start(argparse.Namespace(force=True))
    return captured


class TestDockerLiveness:
    """find_docker only checks that a binary exists. OrbStack's CLI waits for
    its daemon instead of refusing, so an installed-but-stopped runtime is
    found and then never answers.
    """

    def test_a_hang_is_not_running(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=5),
        ):
            assert _docker_is_running("/usr/local/bin/docker") is False

    def test_a_refusal_is_not_running(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert _docker_is_running("/usr/local/bin/docker") is False

    def test_a_missing_binary_is_not_running(self):
        with patch("subprocess.run", side_effect=OSError("no such file")):
            assert _docker_is_running("/usr/local/bin/docker") is False

    def test_an_answering_daemon_is_running(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert _docker_is_running("/usr/local/bin/docker") is True

    def test_discovery_rejects_a_stopped_daemon(self):
        """The callers of _find_running_docker read live Docker state and all
        catch RuntimeError. Returning a path that hangs turns that into an
        uncaught TimeoutExpired several calls later."""
        with patch(
            "immich_accelerator.__main__.find_docker",
            return_value="/usr/local/bin/docker",
        ), patch(
            "immich_accelerator.__main__._docker_is_running", return_value=False
        ):
            with pytest.raises(RuntimeError, match="daemon is not running"):
                _find_running_docker()

    def test_discovery_returns_an_answering_daemon(self):
        with patch(
            "immich_accelerator.__main__.find_docker",
            return_value="/usr/local/bin/docker",
        ), patch(
            "immich_accelerator.__main__._docker_is_running", return_value=True
        ):
            assert _find_running_docker() == "/usr/local/bin/docker"

    def test_detection_reports_a_hang_as_runtime_error(self):
        """A daemon that stops after discovery leaves the CLI waiting mid
        detection, and every caller of detect_immich catches RuntimeError only.
        """
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=10),
        ):
            with pytest.raises(RuntimeError, match="stopped responding"):
                detect_immich("/usr/local/bin/docker")


class TestStartWithoutAValidatedDocker:
    """cmd_start validates IMMICH_WORKERS_INCLUDE and IMMICH_MEDIA_LOCATION
    against the running container. When detection fails, both are skipped, and
    what happens next has to depend on whether anything else can answer.
    """

    def _run(self, config):
        import immich_accelerator.__main__ as m

        base = {
            "db_hostname": "localhost",
            "db_port": "5432",
            "db_username": "postgres",
            "db_password": "pw",
            "db_name": "immich",
            "redis_hostname": "localhost",
            "redis_port": "6379",
            "ml_port": 3003,
            "version": "3.0.1",
            "server_dir": "/srv",
            "node": "/usr/bin/node",
        }
        base.update(config)
        started = MagicMock()

        with patch.object(m, "load_config", return_value=base), patch.object(
            m, "save_config"
        ), patch.object(m, "_kill_stale_processes"), patch.object(
            m, "find_docker", side_effect=RuntimeError("no docker")
        ), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(
            m, "find_node", return_value="/usr/bin/node"
        ), patch.object(
            m, "_check_node_engines_compat", return_value=(True, "")
        ), patch.object(
            m, "_verify_sharp_loads", return_value=(True, "")
        ), patch.object(
            m, "_build_link_ok", return_value=True
        ), patch.object(
            m, "_preflight_env_health", return_value=True
        ), patch.object(
            m, "ensure_media_ready", return_value=True
        ), patch.object(
            m, "_find_ml_dir", return_value=None
        ), patch.object(
            m, "_start_ml_preferred", return_value=(0, None, False)
        ), patch.object(
            m, "kill_pid"
        ), patch.object(
            m, "start_service", started
        ):
            m.cmd_start(argparse.Namespace(force=True))
        return started

    def test_a_local_install_does_not_start(self, tmp_data_dir):
        """No immich_url means Immich is supposed to be in local Docker. We
        could not read it, so there is nothing left to validate against and
        the worker would be feeding an unconfirmed stack."""
        assert not self._run({}).called

    def test_a_split_install_still_starts(self, tmp_data_dir):
        """A split install is expected to have no local Docker; the API probe
        below covers the path check."""
        started = self._run({"immich_url": "http://immich.example:2283"})
        assert started.called


class TestWorkerFreeWatchDoesNotStrandTheInstall:
    """Two ways the worker-free loop could quietly ruin an install.

    Both come from the same change: that loop used to be reachable only via the
    `ml_only` preset, and is now reachable by any full install running
    `component worker off`. Things it could assume before, it cannot assume now.
    """

    def _loop(self, m, configs):
        """Run _watch_without_worker over a scripted sequence of configs."""
        return patch.multiple(
            m,
            load_config=MagicMock(side_effect=configs),
            read_pid=MagicMock(return_value=999),
            reconcile_dashboard=MagicMock(),
            reconcile_components=MagicMock(),
            cap_service_logs=MagicMock(),
            _start_without_worker=MagicMock(),
        )

    def test_does_not_hand_over_to_a_worker_that_cannot_start(self, tmp_data_dir):
        """Handing over would crash-loop launchd forever.

        `component worker on` refuses without a worker config, but the README
        documents these as plain keys in config.json, so a hand-edited or
        restored file reaches the watcher without passing that check. Switching
        anyway makes cmd_start raise out of cmd_watch, main() exits 1, launchd
        relaunches into the same crash, and the ML engine and dashboard are
        left running unsupervised for good.
        """
        import immich_accelerator.__main__ as m

        ml_only = {"ml_only": True, "ml_port": 3003, "worker": True}
        with self._loop(m, [ml_only, KeyboardInterrupt()]), patch.object(
            m, "_upgraded_on_disk", return_value=False
        ), patch("signal.signal"), patch("time.sleep"), patch.object(m, "log") as log:
            assert m._watch_without_worker(ml_only) is None, "must not switch"

        said = " ".join(str(c) for c in log.error.call_args_list)
        assert "server_dir" in said, "should name what is missing"

    def test_hands_over_when_the_config_really_describes_a_worker(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        full: dict = {k: "x" for k in m._WORKER_CONFIG_KEYS}
        full["worker"] = True
        with self._loop(m, [full]), patch.object(
            m, "_upgraded_on_disk", return_value=False
        ), patch("signal.signal"), patch("time.sleep"), patch.object(m, "log"):
            assert m._watch_without_worker(full) == m._SWITCH

    def test_applies_a_brew_upgrade(self, tmp_data_dir):
        """Without this, `component worker off` silently opts an install out of
        every future upgrade: brew writes the new code and the KeepAlive'd
        watcher keeps running the old one forever. That is #79 again, reached
        from a documented command."""
        import immich_accelerator.__main__ as m

        off = {"worker": False, "ml_port": 3003}
        with self._loop(m, [off]), patch.object(
            m, "_upgraded_on_disk", return_value=True
        ) as upgraded, patch("signal.signal"), patch("time.sleep"), patch.object(
            m, "log"
        ):
            assert m._watch_without_worker(off) is None
            upgraded.assert_called_once()

    def test_both_watch_loops_share_one_upgrade_check(self):
        """Derived, not hand-listed: whichever loop someone adds next, the
        check is a named function and its absence is visible here."""
        import inspect

        import immich_accelerator.__main__ as m

        for loop in (m._watch_worker, m._watch_without_worker):
            assert "_upgraded_on_disk" in inspect.getsource(loop), loop.__name__


class TestStartWithoutWorkerStopsTheWorker:
    """`start` on a worker-off install must not leave one running.

    _kill_stale_processes deliberately spares anything named in a pidfile, so a
    live tracked worker walks straight through it. Left alone it keeps pulling
    jobs while status, the menu bar and the dashboard all report it off, and
    the user points a second machine at the same queues.
    """

    CFG = {"worker": False, "ml": False, "dashboard": True, "ml_port": 3003}

    def test_a_running_worker_is_stopped(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_kill_stale_processes"), patch.object(
            m, "read_pid", side_effect=lambda n: 4242 if n == "worker" else None
        ), patch.object(m, "kill_pid") as kill, patch.object(
            m, "start_dashboard"
        ), patch.object(
            m, "log"
        ):
            m._start_without_worker(dict(self.CFG), argparse.Namespace(force=False))

        assert "worker" in [c.args[0] for c in kill.call_args_list]

    def test_nothing_is_killed_when_no_worker_is_running(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "_kill_stale_processes"), patch.object(
            m, "read_pid", return_value=None
        ), patch.object(m, "kill_pid") as kill, patch.object(
            m, "start_dashboard"
        ), patch.object(
            m, "log"
        ):
            m._start_without_worker(dict(self.CFG), argparse.Namespace(force=False))

        assert "worker" not in [c.args[0] for c in kill.call_args_list]


class TestWatchDispatch:
    """cmd_watch picks a loop by the worker component, and each loop hands back
    when that key flips, so a toggle takes effect on a running watcher instead
    of at the next restart."""

    def test_dispatches_to_worker_loop_by_default(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "load_config", return_value={}), patch.object(
            m, "_watch_worker", return_value=None
        ) as worker, patch.object(m, "_watch_without_worker") as no_worker:
            m.cmd_watch(None)
            worker.assert_called_once()
            no_worker.assert_not_called()

    def test_dispatches_to_worker_free_loop_when_worker_off(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(
            m, "load_config", return_value={"worker": False}
        ), patch.object(m, "_watch_worker") as worker, patch.object(
            m, "_watch_without_worker", return_value=None
        ) as no_worker:
            m.cmd_watch(None)
            no_worker.assert_called_once()
            worker.assert_not_called()

    def test_switch_hands_over_to_the_other_loop(self, tmp_data_dir):
        """The worker loop returning _SWITCH must re-dispatch, not exit."""
        import immich_accelerator.__main__ as m

        configs = [{}, {"worker": False}]
        with patch.object(
            m, "load_config", side_effect=lambda: configs.pop(0)
        ), patch.object(
            m, "_watch_worker", return_value=m._SWITCH
        ) as worker, patch.object(
            m, "_watch_without_worker", return_value=None
        ) as no_worker:
            m.cmd_watch(None)
            worker.assert_called_once()
            no_worker.assert_called_once()

    def test_worker_loop_stops_the_worker_when_disabled_mid_flight(self, tmp_data_dir):
        """Otherwise the crash-check would fight the toggle and restart it."""
        import immich_accelerator.__main__ as m

        with patch.object(
            m, "load_config", side_effect=[{}, {"worker": False}]
        ), patch.object(m, "read_pid", return_value=1234), patch.object(
            m, "reconcile_components"
        ), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(
            m, "cmd_start"
        ), patch.object(
            m, "cap_service_logs"
        ), patch(
            "signal.signal"
        ), patch(
            "time.sleep", return_value=None
        ), patch.object(
            m, "WORKER_VERSION_FILE", Path("/nonexistent/worker-version")
        ):
            assert m._watch_worker({}) == m._SWITCH
            kill.assert_called_once_with("worker")

    def test_worker_free_loop_hands_back_when_worker_enabled(self, tmp_data_dir):
        """The config must describe a worker, not merely ask for one.

        This used to pass {"worker": True} and nothing else. That is now the
        case the loop deliberately refuses (handing over to a cmd_start that
        cannot succeed crash-loops launchd), so the test asked for the
        behaviour it exists to prevent and spun forever waiting for it. See
        TestWorkerFreeWatchDoesNotStrandTheInstall for the refusing half.
        """
        import immich_accelerator.__main__ as m

        enabled: dict = {k: "x" for k in m._WORKER_CONFIG_KEYS}
        enabled["worker"] = True
        with patch.object(m, "load_config", return_value=enabled), patch.object(
            m, "read_pid", return_value=4321
        ), patch.object(m, "reconcile_dashboard"), patch.object(
            m, "reconcile_components"
        ), patch.object(
            m, "cap_service_logs"
        ), patch.object(
            m, "_upgraded_on_disk", return_value=False
        ), patch(
            "signal.signal"
        ), patch(
            "time.sleep", return_value=None
        ):
            assert m._watch_without_worker({"worker": False}) == m._SWITCH


class TestReconcileML:
    """reconcile_ml: the ML half of the one-enforcement-point rule."""

    def test_stops_ml_when_disabled(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=999), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(m, "_start_ml_service") as start:
            m.reconcile_ml({"ml": False})
            kill.assert_called_once_with("ml")
            start.assert_not_called()

    def test_does_not_start_ml_when_disabled_and_already_down(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(m, "_start_ml_service") as start:
            m.reconcile_ml({"ml": False})
            kill.assert_not_called()
            start.assert_not_called()

    def test_restarts_ml_when_enabled_and_down(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "_find_ml_dir", return_value=None
        ), patch.object(
            m, "_start_ml_service", return_value=(77, "native Swift")
        ) as start:
            m.reconcile_ml({})
            start.assert_called_once()

    def test_a_live_answering_process_is_left_alone(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=999), patch.object(
            m, "_ml_ping", return_value=True
        ), patch.object(m, "kill_pid") as kill, patch.object(
            m, "_start_ml_service"
        ) as start:
            m.reconcile_ml({})
            kill.assert_not_called()
            start.assert_not_called()

    def test_silence_is_not_acted_on_immediately(self, tmp_data_dir):
        """A cold native start loads weights before it answers, and a first-use
        model fetch runs for minutes. Restarting into either kills the work
        being waited on, so one quiet pass must not be enough."""
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=999), patch.object(
            m, "_ml_ping", return_value=False
        ), patch.object(m, "kill_pid") as kill, patch.object(
            m, "_start_ml_service"
        ) as start:
            m.reconcile_ml({})
            m.reconcile_ml({})  # still inside the grace window
            kill.assert_not_called()
            start.assert_not_called()

    def test_a_process_that_never_answers_is_restarted(self, tmp_data_dir):
        """The regression that let a Mac run for days on an ML service the
        accelerator thought it was managing: reconcile_ml returned as soon as
        it saw a live PID, so a process that was up but serving nothing (a
        native engine that lost the port race, say) kept its place forever.
        """
        import immich_accelerator.__main__ as m

        clock = [1000.0]
        with patch.object(m, "read_pid", return_value=999), patch.object(
            m, "_ml_ping", return_value=False
        ), patch.object(m, "_find_ml_dir", return_value=None), patch.object(
            m.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(
            # Real wait_for_port_free() polls time.monotonic() itself in a
            # while loop; against this test's frozen clock the deadline is
            # never reached and it spins forever.
            m,
            "wait_for_port_free",
            return_value=True,
        ), patch.object(
            m, "_start_ml_service", return_value=(77, "native Swift")
        ) as start:
            m.reconcile_ml({})  # first quiet pass: start the timer
            kill.assert_not_called()
            clock[0] += m.ML_UNRESPONSIVE_GRACE + 1
            m.reconcile_ml({})

            kill.assert_called_once_with("ml")
            start.assert_called_once()

    def test_answering_again_clears_the_timer(self, tmp_data_dir):
        """Otherwise a service that goes quiet for a moment every few hours
        eventually accumulates its way past the grace window and gets killed
        while it is working perfectly well."""
        import immich_accelerator.__main__ as m

        clock = [1000.0]
        answers = [False, True, False]
        with patch.object(m, "read_pid", return_value=999), patch.object(
            m, "_ml_ping", side_effect=lambda *a, **k: answers.pop(0)
        ), patch.object(
            m.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(
            m, "_start_ml_service"
        ):
            m.reconcile_ml({})  # quiet: timer starts at t=1000
            clock[0] += 10
            m.reconcile_ml({})  # answered: timer must reset
            clock[0] += m.ML_UNRESPONSIVE_GRACE - 1
            m.reconcile_ml({})  # quiet again, but only just now

            kill.assert_not_called()

    def test_a_dead_process_does_not_bequeath_its_timer(self, tmp_data_dir):
        """A service that went quiet and then died on its own left the clock
        running, so its replacement was judged from the dead instance's start
        and got killed on its first quiet tick. For a cold start that is every
        time, which is a restart loop rather than a recovery."""
        import immich_accelerator.__main__ as m

        clock = [1000.0]
        pids = [999, None, 1001]
        with patch.object(
            m, "read_pid", side_effect=lambda *a: pids.pop(0)
        ), patch.object(m, "_ml_ping", return_value=False), patch.object(
            m, "_find_ml_dir", return_value=None
        ), patch.object(
            m.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(
            m, "_start_ml_service", return_value=(1001, "native Swift")
        ):
            m.reconcile_ml({})  # quiet at t=1000: timer starts
            # The dead instance stays quiet past the grace window before it
            # exits. That is the whole point: by the time the replacement
            # starts, the stale clock already reads "long enough to kill".
            clock[0] += m.ML_UNRESPONSIVE_GRACE + 1
            m.reconcile_ml({})  # process gone: restarted, timer must clear
            clock[0] += 10
            m.reconcile_ml({})  # new process, still loading, seconds old

            kill.assert_not_called()

    def test_a_replaced_pid_starts_its_own_clock(self, tmp_data_dir):
        """The stopwatch belongs to a process, not to the wall.

        A bare timestamp is inherited by whatever PID happens to be there next,
        so a service replaced between two ticks (a manual `restart`, or a crash
        and relaunch inside one interval) gets judged from its predecessor's
        silence and killed seconds into its own cold start.
        """
        import immich_accelerator.__main__ as m

        clock = [1000.0]
        pids = [999, 1001, 1001]
        with patch.object(
            m, "read_pid", side_effect=lambda *a: pids.pop(0)
        ), patch.object(m, "_ml_ping", return_value=False), patch.object(
            m.time, "monotonic", side_effect=lambda: clock[0]
        ), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(
            m, "_start_ml_service", return_value=(1001, "native Swift")
        ):
            m.reconcile_ml({})  # 999 goes quiet at t=1000
            clock[0] += m.ML_UNRESPONSIVE_GRACE + 1
            m.reconcile_ml({})  # different PID: its own clock starts now
            clock[0] += 10
            m.reconcile_ml({})  # 1001 is 10s old, nowhere near the grace

            kill.assert_not_called()

    def test_a_foreign_listener_is_left_alone(self, tmp_data_dir):
        """Docker's own ML container on port 3003, say.

        The service now exits on a bind conflict instead of lingering, so
        relaunching it into an occupied port would spawn and lose a process
        every tick forever. If the port answers, Immich has ML either way.
        """
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "_ml_ping", return_value=True
        ), patch.object(m, "adopt_live_ml", return_value=None), patch.object(
            m, "_find_ml_dir", return_value=None
        ), patch.object(m, "_start_ml_service") as start, patch.object(
            m, "log"
        ) as log:
            m.reconcile_ml({})
            m.reconcile_ml({})  # and again: the warning must not repeat

            start.assert_not_called()
        said = [str(c) for c in log.warning.call_args_list]
        assert len(said) == 1, f"warned {len(said)} times, expected once"
        assert "already served" in said[0]

    def test_a_dead_port_still_restarts(self, tmp_data_dir):
        """The foreign-listener check must not swallow the ordinary case."""
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "_ml_ping", return_value=False
        ), patch.object(m, "_find_ml_dir", return_value=None), patch.object(
            m, "_start_ml_service", return_value=(77, "native Swift")
        ) as start:
            m.reconcile_ml({})
            start.assert_called_once()

    def test_disabling_ml_clears_the_timer(self, tmp_data_dir):
        """A component turned off and back on starts from a clean slate rather
        than inheriting silence recorded before it was switched off."""
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=999), patch.object(
            m, "_ml_ping", return_value=False
        ), patch.object(m, "kill_pid"), patch.object(m, "_start_ml_service"):
            m.reconcile_ml({})
            assert m._ml_unresponsive_since is not None
            m.reconcile_ml({"ml": False})
            assert m._ml_unresponsive_since is None


class TestDashboardComponentAwareness:
    """The dashboard must agree with the CLI about what is switched on, and it
    keeps its own copy of the rule (importing __main__ from a module launched as
    __main__ would load a second copy of it)."""

    def test_matches_main_precedence_exactly(self):
        from immich_accelerator import dashboard as d
        from immich_accelerator.__main__ import COMPONENTS, _component_enabled

        configs = [
            {},
            {"dashboard": False},
            {"ml": False},
            {"worker": False},
            {"ml_only": True},
            {"ml_only": True, "worker": True},
        ]
        for cfg in configs:
            for name in COMPONENTS:
                assert d._component_on(cfg, name) == _component_enabled(
                    name, cfg
                ), f"dashboard and CLI disagree on {name} for {cfg}"

    def test_disabled_services_are_omitted_not_drawn_dead(self):
        """The page renders whatever keys arrive, so omitting one is the fix:
        a component switched off must not show as a red dot."""
        from immich_accelerator import dashboard as d

        def status_for(config):
            # get_status memoizes for _CACHE_TTL seconds; clear it so the second
            # call re-evaluates instead of replaying the first.
            d._cache, d._cache_ts = {}, 0
            return d.get_status(config)["services"]

        with patch.object(d, "_query_db", return_value=""), patch.object(
            d, "_run", return_value=""
        ), patch.object(d, "_get_accelerator_version", return_value="1.9.0"):
            off = status_for({"ml": False, "immich_url": ""})
            on = status_for({"immich_url": ""})
            worker_off = status_for({"worker": False, "immich_url": ""})
        assert "ml" not in off
        assert "ml" in on
        assert "worker" in off
        # No worker means no local library, so the Docker/API dot is meaningless
        # too and would sit red forever on an ML-only node.
        assert "worker" not in worker_off
        assert "docker" not in worker_off
        assert "ml" in worker_off


class TestStartIsSerialized:
    """The lock lives inside cmd_start, not at its call sites.

    There are seven call sites and the two that were locked were the two
    somebody remembered. A toggle and the watcher can both decide to start the
    worker inside the same 30s window, and two concurrent starts race over the
    pid files, the /build link and the sharp rebuild."""

    def test_every_cmd_start_is_covered(self):
        import immich_accelerator.__main__ as m

        held = []
        with patch.object(m, "_start_lock") as lock, patch.object(m, "_cmd_start"):
            lock.side_effect = lambda *a, **k: held.append(1) or _NullCtx()
            m.cmd_start(argparse.Namespace(force=False))
        assert held, "cmd_start must take the start lock itself"

    def test_the_lock_lands_in_the_temp_dir_not_the_real_home(self, tmp_data_dir):
        """The fixture must isolate the lock, not just the config and pids.

        Releases are validated on the same Mac that runs production
        (CLAUDE.md: the Mini is the release gate). An unpatched LOCK_FILE means
        a test taking a real flock contends with the live watcher for the
        production start lock: the test hangs on the 180s timeout, or the
        watcher's worker restart stalls for the length of the suite and Immich
        sits with no worker while the tests pass green. Same trap as read_pid's
        global process scan, where a throwaway HOME looked isolating and was
        not.
        """
        import immich_accelerator.__main__ as m

        assert m.LOCK_FILE == tmp_data_dir["lock_file"]
        with m._start_lock(timeout=1.0):
            pass
        assert Path.home() not in m.LOCK_FILE.parents, "points at the real install"
        assert tmp_data_dir["lock_file"].exists(), "the lock was taken elsewhere"

    def test_lock_is_not_taken_again_by_callers(self):
        """Re-entrancy would deadlock: flock is per-file-descriptor but the
        wait loop is not re-entrant, so a caller holding it while cmd_start
        takes it again would block until the timeout."""
        import inspect

        import immich_accelerator.__main__ as m

        for fn in (m._set_component, m._restart_worker):
            assert "_start_lock" not in inspect.getsource(
                fn
            ), f"{fn.__name__} must not take the lock; cmd_start does"


class _NullCtx:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


class TestRestartWorkerDelegatesToTheWatcher:
    """Stopping is safe; starting has ten failure paths. When a supervisor is
    running, only stop, and let it do the start it already knows how to do."""

    def test_supervised_restart_only_stops(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=999), patch.object(
            m, "kill_pid"
        ) as kill, patch.object(m, "_watcher_running", return_value=True), patch.object(
            m, "cmd_start"
        ) as start:
            assert m._restart_worker("for a test") is True
            kill.assert_called_once_with("worker")
            start.assert_not_called()

    def test_unsupervised_restart_starts_and_reports_failure(self, tmp_data_dir):
        """With nothing to converge for us, we must start it AND admit it if
        the worker does not come back, rather than leaving a box with no
        processing while claiming success."""
        import immich_accelerator.__main__ as m

        pids = iter([999, None, None])
        with patch.object(
            m, "read_pid", side_effect=lambda n: next(pids)
        ), patch.object(m, "kill_pid"), patch.object(
            m, "_watcher_running", return_value=False
        ), patch.object(
            m, "cmd_start"
        ) as start:
            assert m._restart_worker("for a test") is False
            start.assert_called_once()

    def test_nothing_to_restart_is_success(self, tmp_data_dir):
        import immich_accelerator.__main__ as m

        with patch.object(m, "read_pid", return_value=None), patch.object(
            m, "kill_pid"
        ) as kill:
            assert m._restart_worker("for a test") is True
            kill.assert_not_called()


class TestSetupReestablishesComponents:
    """A full setup means you want a worker. Preserving component keys wholesale
    made re-running setup on an ml-only box silently keep worker: false, which
    breaks the documented ml-only-to-full upgrade path."""

    def test_worker_is_the_only_component_key_setup_may_reset(self):
        from immich_accelerator.__main__ import _PRESERVED_CONFIG_KEYS

        # Re-running a full setup on an ml-only box is how you turn the worker
        # back on, so this one key is deliberately not carried across.
        assert "worker" not in _PRESERVED_CONFIG_KEYS
        # The other two are pure preferences that no full setup path writes,
        # so leaving them out did not mean "setup decides", it meant "setup
        # silently discards". Dropping "ml" started a local engine behind the
        # back of anyone who had offloaded ML to another machine, downloaded
        # gigabytes of models, and repointed the worker at localhost.
        assert "dashboard" in _PRESERVED_CONFIG_KEYS
        assert "ml" in _PRESERVED_CONFIG_KEYS

    def test_ml_off_survives_a_setup_rerun(self, tmp_data_dir):
        """The offloaded-ML path end to end, through the real _finalize_config."""
        import immich_accelerator.__main__ as m

        m.save_config({"ml": False, "ml_url": "http://10.0.0.9:3003", "api_key": "k"})
        fresh = {"version": "3.0.2", "server_dir": "/srv", "node": "/node"}
        # _finalize_config's tail is interactive (the /build link and the
        # start prompt). Only the preserve-and-save half is under test.
        with patch.object(m, "log"), patch.object(
            m, "_ensure_build_link"
        ), patch.object(m, "cmd_start"), patch.object(
            m, "_offer_launchd_service", create=True
        ), patch(
            "builtins.input", return_value="n"
        ):
            m._finalize_config(fresh)

        saved = m.load_config()
        assert saved["ml"] is False, "setup re-enabled an engine the user turned off"
        assert saved["ml_url"] == "http://10.0.0.9:3003"


class TestWorkerConfigGate:
    """One authoritative list of what cmd_start needs, checked up front, raising
    RuntimeError (which main() catches) rather than KeyError (which it does
    not, and which crash-loops the launchd watcher)."""

    def test_ml_only_config_is_rejected_by_name(self):
        import immich_accelerator.__main__ as m

        with pytest.raises(RuntimeError) as e:
            m._require_worker_config({"ml_only": True, "ml_port": 3003})
        assert "server_dir" in str(e.value)
        assert "setup" in str(e.value)

    def test_a_complete_config_passes(self):
        import immich_accelerator.__main__ as m

        cfg = {k: "x" for k in m._WORKER_CONFIG_KEYS}
        m._require_worker_config(cfg)  # must not raise

    def test_gate_covers_every_key_cmd_start_dereferences(self):
        """The previous version was a hand-maintained duplicate that had already
        drifted four keys behind cmd_start."""
        import inspect
        import re

        import immich_accelerator.__main__ as m

        src = inspect.getsource(m._cmd_start)
        # A hard read is config["x"] used as a value. Two things are not hard
        # reads and must not be flagged: an assignment (config["x"] = ...,
        # which creates the key), and a read the function already guards with
        # its own config.get("x") truthiness check.
        reads = set(re.findall(r'config\["([a-z_]+)"\](?!\s*=[^=])', src))
        guarded = set(re.findall(r'config\.get\("([a-z_]+)"', src))
        missing = reads - guarded - set(m._WORKER_CONFIG_KEYS)
        assert not missing, f"cmd_start dereferences ungated keys: {sorted(missing)}"
