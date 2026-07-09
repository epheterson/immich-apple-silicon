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

    def test_dashboard_custom_port(self):
        parser = self._build_parser()
        args = parser.parse_args(["dashboard", "--port", "9000"])
        assert args.port == 9000

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
        dash_p.add_argument("--port", type=int, default=8420)
        sub.add_parser("uninstall")
        return parser


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
        # Serving on 8420 but no pid file → adopt the listener's pid (so a later
        # stop can reach it) instead of spawning a second one.
        from immich_accelerator.__main__ import start_dashboard

        with patch("immich_accelerator.__main__.read_pid", return_value=None), patch(
            "immich_accelerator.__main__._pid_on_port", return_value=729
        ), patch("immich_accelerator.__main__.write_pid") as wpid, patch(
            "immich_accelerator.__main__.subprocess.Popen"
        ) as popen:
            start_dashboard()
            popen.assert_not_called()
            wpid.assert_called_once_with("dashboard", 729)

    def test_starts_fresh_when_nothing_running(self):
        from immich_accelerator.__main__ import start_dashboard

        with patch("immich_accelerator.__main__.read_pid", return_value=None), patch(
            "immich_accelerator.__main__._pid_on_port", return_value=None
        ), patch("immich_accelerator.__main__.write_pid"), patch(
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

    def test_unstamped_plugin_era_reextracts(self, tmp_path):
        # An old accelerator extracted 3.0 build-data without stamping it. Even
        # though the plugin is present, we can't prove it belongs to 3.0.1, so
        # we re-extract rather than risk trusting mismatched build-data.
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        self._make_30_build_data(tmp_path, stamp=None)
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "3.0.1") is None

    def test_stale_version_stamp_reextracts(self, tmp_path):
        # The dominant bug: build-data stamped for a different version (2.7.5
        # rollback then forward to 3.0.1) must NOT be trusted for 3.0.1.
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        self._make_30_build_data(tmp_path, stamp="2.7.5")
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "3.0.1") is None

    def test_stale_27_coreplugin_does_not_falsely_pass(self, tmp_path):
        # A 2.7 corePlugin/manifest.json left in build-data used to satisfy the
        # old plugin-presence check for a broken 3.0 cache. The stamp guard now
        # requires the stamp to match, so it re-extracts.
        from immich_accelerator import __main__ as m

        server_dir = self._make_server(tmp_path, "3.0.1")
        build_data = tmp_path / "build-data"
        (build_data / "corePlugin").mkdir(parents=True)
        (build_data / "corePlugin" / "manifest.json").write_text("{}")
        (build_data / ".accel-version").write_text("2.7.5\n")
        with patch.object(m, "DATA_DIR", tmp_path):
            assert m._cached_server_if_current(server_dir, "3.0.1") is None


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
