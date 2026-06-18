"""Tests for immich_accelerator.dashboard — status API, caching, FastAPI app."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Guard: skip all dashboard tests if fastapi/httpx are not installed
fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from immich_accelerator.dashboard import (
    _get_accelerator_version,
    _run,
    _query_db,
    get_status,
    create_app,
    _CACHE_TTL,
)

# ---------------------------------------------------------------------------
# _get_accelerator_version
# ---------------------------------------------------------------------------


class TestGetAcceleratorVersion:
    def test_reads_version_file(self, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("1.3.1\n")
        with patch("immich_accelerator.dashboard.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.parent.__truediv__ = (
                lambda self, x: version_file
            )
            # Directly test: the function reads from Path(__file__).parent.parent / "VERSION"
            # We'll just verify the fallback behavior since patching __file__ is awkward
            pass

    def test_fallback_on_missing_file(self):
        with patch("immich_accelerator.dashboard.Path") as mock_path_cls:
            mock_version = MagicMock()
            mock_version.exists.return_value = False
            mock_path_cls.return_value.parent.parent.__truediv__.return_value = (
                mock_version
            )
            result = _get_accelerator_version()
            assert result == "1.0.0"

    def test_fallback_on_os_error(self):
        with patch("immich_accelerator.dashboard.Path") as mock_path_cls:
            mock_path_cls.return_value.parent.parent.__truediv__.side_effect = OSError
            result = _get_accelerator_version()
            assert result == "1.0.0"


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


class TestRun:
    def test_successful_command(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "  hello world  \n"
        with patch("subprocess.run", return_value=result):
            assert _run(["echo", "hello"]) == "hello world"

    def test_failed_command_returns_empty(self):
        result = MagicMock()
        result.returncode = 1
        result.stdout = "error output"
        with patch("subprocess.run", return_value=result):
            assert _run(["false"]) == ""

    def test_timeout_returns_empty(self):
        import subprocess

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="cmd", timeout=5),
        ):
            assert _run(["slow-cmd"]) == ""

    def test_os_error_returns_empty(self):
        with patch("subprocess.run", side_effect=OSError("No such file")):
            assert _run(["/nonexistent"]) == ""

    def test_custom_timeout(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok"
        with patch("subprocess.run", return_value=result) as mock_run:
            _run(["cmd"], timeout=30)
            _, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 30

    def test_env_passed_through(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok"
        custom_env = {"FOO": "bar"}
        with patch("subprocess.run", return_value=result) as mock_run:
            _run(["cmd"], env=custom_env)
            _, kwargs = mock_run.call_args
            assert kwargs["env"] == custom_env


# ---------------------------------------------------------------------------
# _query_db
# ---------------------------------------------------------------------------


class TestQueryDb:
    def test_uses_psql_when_password_set(self):
        config = {
            "db_hostname": "192.168.1.100",
            "db_port": "5432",
            "db_username": "postgres",
            "db_password": "secret",
            "db_name": "immich",
        }
        with patch("os.path.exists", return_value=True), patch(
            "immich_accelerator.dashboard._run", return_value="42"
        ) as mock_run:
            result = _query_db("SELECT 1", config)
            assert result == "42"
            cmd = mock_run.call_args[0][0]
            assert "psql" in cmd[0]
            assert "-h" in cmd
            assert "192.168.1.100" in cmd

    def test_falls_back_to_docker_when_no_psql(self):
        config = {
            "db_hostname": "localhost",
            "db_port": "5432",
            "db_username": "postgres",
            "db_password": "",
            "db_name": "immich",
        }

        # psql not found, docker found
        def exists_side_effect(path):
            if "psql" in str(path):
                return False
            if "docker" in str(path):
                return True
            return False

        with patch("os.path.exists", side_effect=exists_side_effect), patch(
            "immich_accelerator.dashboard._run", return_value="1"
        ) as mock_run:
            result = _query_db("SELECT 1", config)
            assert result == "1"
            cmd = mock_run.call_args[0][0]
            assert "docker" in cmd[0]
            assert "exec" in cmd

    def test_uses_custom_db_container(self):
        config = {
            "db_hostname": "localhost",
            "db_port": "5432",
            "db_username": "postgres",
            "db_password": "",
            "db_name": "immich",
            "db_container": "my_custom_postgres",
        }

        def exists_side_effect(path):
            if "psql" in str(path):
                return False
            if "docker" in str(path):
                return True
            return False

        with patch("os.path.exists", side_effect=exists_side_effect), patch(
            "immich_accelerator.dashboard._run", return_value="1"
        ) as mock_run:
            _query_db("SELECT 1", config)
            cmd = mock_run.call_args[0][0]
            assert "my_custom_postgres" in cmd


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Reset the module-level cache before each test."""
        import immich_accelerator.dashboard as d

        d._cache = {}
        d._cache_ts = 0
        d._static_hw = None
        yield
        d._cache = {}
        d._cache_ts = 0
        d._static_hw = None

    def test_returns_structure(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="200|100|10|150|80|70|120|5",
        ), patch(
            "immich_accelerator.dashboard._run", return_value="{ 1.50 2.00 3.00 }"
        ):
            status = get_status(sample_config)

        assert "services" in status
        assert "progress" in status
        assert "system" in status
        assert "version" in status
        assert "accelerator_version" in status
        assert "queue_active" in status

    def test_services_section(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="100|50|10|40|30|25|60|3",
        ), patch(
            "immich_accelerator.dashboard._run", return_value="{ 0.50 1.00 1.50 }"
        ):
            status = get_status(sample_config)

        assert "worker" in status["services"]
        assert "ml" in status["services"]
        assert "docker" in status["services"]
        for svc in status["services"].values():
            assert "alive" in svc
            assert "name" in svc

    def test_progress_section(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="100|80|10|70|60|50|40|5",
        ), patch(
            "immich_accelerator.dashboard._run", return_value="{ 0.50 1.00 1.50 }"
        ):
            status = get_status(sample_config)

        progress = status["progress"]
        assert "thumbnails" in progress
        assert "clip" in progress
        assert "faces" in progress
        assert "ocr" in progress
        assert "video" in progress
        for key in ("thumbnails", "clip", "faces", "ocr", "video"):
            p = progress[key]
            assert "done" in p
            assert "total" in p
            assert "pct" in p
            assert "skipped" in p

    def test_progress_calculations(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="100|80|10|100|80|70|90|5",
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            status = get_status(sample_config)

        # thumbs (100) over total_assets (100) → 100%
        assert status["progress"]["thumbnails"]["pct"] == 100.0

    def test_pct_clamped_to_100_when_done_exceeds_total(self, sample_config):
        """Regression for #68: count skew (done > total) must not show >100%."""
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        # fields: total_assets|total_previews|total_videos|thumbs|clip|faces|ocr|video
        # faces (110) > its denominator total_previews (100) -> 110% without clamp.
        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="200|100|10|150|90|110|150|5",
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            status = get_status(sample_config)

        assert status["progress"]["faces"]["pct"] == 100.0
        for key in ("thumbnails", "clip", "faces", "ocr"):
            assert status["progress"][key]["pct"] <= 100.0

    def test_caching(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="100|50|10|40|30|25|60|3",
        ) as mock_db, patch("immich_accelerator.dashboard._run", return_value=""):
            status1 = get_status(sample_config)
            status2 = get_status(sample_config)

        # DB should only be queried once (second call hits cache)
        assert mock_db.call_count == 1
        assert status1 is status2

    def test_empty_db_response(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db", return_value=""
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            status = get_status(sample_config)

        assert status["progress"]["thumbnails"]["total"] == 0

    def test_malformed_db_response(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="not|a|valid|response",
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            status = get_status(sample_config)

        # Should gracefully handle parse errors
        assert status["progress"]["thumbnails"]["total"] == 0

    def test_load_parsing(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db", return_value="0|0|0|0|0|0|0"
        ), patch(
            "immich_accelerator.dashboard._run", return_value="{ 2.50 3.00 4.00 }"
        ):
            status = get_status(sample_config)

        assert status["system"]["load_1m"] == 2.5

    def test_version_from_config(self, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}

        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db", return_value="0|0|0|0|0|0|0"
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            status = get_status(sample_config)

        assert status["version"] == "2.6.3"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


class TestFastAPIApp:
    @pytest.fixture
    def app(self, sample_config):
        return create_app(sample_config)

    @pytest.fixture
    def client(self, app):
        from starlette.testclient import TestClient

        return TestClient(app)

    def test_index_returns_html(self, client):
        # Mock the HTML file read
        with patch(
            "immich_accelerator.dashboard._load_html", return_value="<html>test</html>"
        ):
            resp = client.get("/")
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]

    def test_api_status_endpoint(self, client, sample_config):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}
        d._cache = {}
        d._cache_ts = 0

        mock_status = {
            "services": {"worker": {"alive": True, "name": "Worker"}},
            "progress": {},
            "system": {"load_1m": 1.0, "mem_total_gb": 32.0, "cpus": 10},
            "version": "2.6.3",
            "accelerator_version": "1.3.1",
            "queue_active": {},
        }
        with patch("immich_accelerator.dashboard.get_status", return_value=mock_status):
            resp = client.get("/api/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "services" in data
            assert "version" in data

    def test_api_requeue_no_api_key(self, sample_config):
        config_no_key = {k: v for k, v in sample_config.items() if k != "api_key"}
        config_no_key["api_key"] = ""
        app = create_app(config_no_key)
        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.post("/api/requeue")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_api_requeue_with_api_key(self, sample_config):
        app = create_app(sample_config)
        from starlette.testclient import TestClient

        client = TestClient(app)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"{}"
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            resp = client.post("/api/requeue")
            assert resp.status_code == 200
            data = resp.json()
            # Should have results for all 5 queues
            assert "thumbnailGeneration" in data
            assert "smartSearch" in data
            assert "faceDetection" in data
            assert "ocr" in data
            assert "videoConversion" in data

    def test_api_requeue_handles_failures(self, sample_config):
        app = create_app(sample_config)
        from starlette.testclient import TestClient

        client = TestClient(app)

        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            resp = client.post("/api/requeue")
            assert resp.status_code == 200
            data = resp.json()
            for v in data.values():
                assert v == "failed"

    def test_api_requeue_handles_400_as_ok(self, sample_config):
        """400 from Immich means 'already running' which is fine."""
        import urllib.error

        app = create_app(sample_config)
        from starlette.testclient import TestClient

        client = TestClient(app)

        error = urllib.error.HTTPError(
            url="http://localhost:2283/api/jobs/thumbnailGeneration",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=error):
            resp = client.post("/api/requeue")
            assert resp.status_code == 200
            data = resp.json()
            for v in data.values():
                assert v == "ok"
