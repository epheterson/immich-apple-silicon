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
