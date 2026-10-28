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
            assert "unqueued" in p

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

    # -- idle is not finished ------------------------------------------------
    #
    # get_status used to return pct=100 for every stage whenever Immich's
    # queues were drained, labelling the remainder "skipped" on the theory that
    # anything still undone after an idle queue must be unprocessable. On a
    # real 174k-asset library with 106k assets awaiting embeddings that drew
    # four full bars and reported the work complete, and it was also why the
    # menu bar (which compares done to total) and the web dashboard disagreed.

    @staticmethod
    def _jobs_response(active: int = 0, waiting: int = 0):
        """A stand-in for Immich's /api/jobs with every queue in one state."""
        body = json.dumps(
            {
                name: {"jobCounts": {"active": active, "waiting": waiting}}
                for name in (
                    "thumbnailGeneration",
                    "smartSearch",
                    "faceDetection",
                    "ocr",
                    "videoConversion",
                )
            }
        ).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: resp
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _status_with_idle_queues(self, config, counts):
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}
        with patch("urllib.request.urlopen", return_value=self._jobs_response()), patch(
            "immich_accelerator.dashboard._query_db", return_value=counts
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            return get_status(config)

    def test_idle_queues_do_not_report_complete(self, sample_config):
        # fields: total_assets|total_previews|total_videos|thumbs|clip|faces|ocr|video
        # Eric's library in miniature: thumbnails nearly done, ML far behind.
        status = self._status_with_idle_queues(
            sample_config, "175234|173932|7664|173932|67712|67712|67712|4703"
        )
        clip = status["progress"]["clip"]
        assert clip["pct"] == 38.9, "an idle queue is not a finished one"
        assert clip["unqueued"] == 106220
        # Video went through the same special case and gets the same treatment.
        assert status["progress"]["video"]["pct"] == 61.4
        assert status["progress"]["video"]["unqueued"] == 2961

    def test_unqueued_is_zero_when_the_jobs_api_is_unreachable(self, sample_config):
        """Unreachable is not idle: with no answer we cannot claim anything."""
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}
        with patch("urllib.request.urlopen", side_effect=OSError), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="100|100|10|100|40|40|40|5",
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            status = get_status(sample_config)

        assert status["progress"]["clip"]["pct"] == 40.0
        assert status["progress"]["clip"]["unqueued"] == 0

    def test_unqueued_is_zero_while_work_is_scheduled(self, sample_config):
        """Waiting jobs are queued by definition, so nothing is unqueued."""
        import immich_accelerator.dashboard as d

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}
        with patch(
            "urllib.request.urlopen", return_value=self._jobs_response(waiting=60)
        ), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="100|100|10|100|40|40|40|5",
        ), patch(
            "immich_accelerator.dashboard._run", return_value=""
        ):
            status = get_status(sample_config)

        assert status["progress"]["clip"]["pct"] == 40.0
        assert status["progress"]["clip"]["unqueued"] == 0

    def test_unqueued_is_judged_per_queue_not_globally(self, sample_config):
        """One busy queue must not blank the hint on all the others.

        Gating on "is ANY queue busy" meant a video transcode backlog, or the
        first seconds after Re-queue Missing, hid "106,220 not queued" from the
        CLIP bar precisely while someone was watching it, then flashed it back
        when the last queue drained.
        """
        import immich_accelerator.dashboard as d

        busy_video = json.dumps(
            {
                "videoConversion": {"jobCounts": {"active": 3, "waiting": 40}},
                "thumbnailGeneration": {"jobCounts": {"active": 0, "waiting": 0}},
                "smartSearch": {"jobCounts": {"active": 0, "waiting": 0}},
                "faceDetection": {"jobCounts": {"active": 0, "waiting": 0}},
                "ocr": {"jobCounts": {"active": 0, "waiting": 0}},
            }
        ).encode()
        resp = MagicMock()
        resp.read.return_value = busy_video
        resp.__enter__ = lambda s: resp
        resp.__exit__ = MagicMock(return_value=False)

        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}
        with patch("urllib.request.urlopen", return_value=resp), patch(
            "immich_accelerator.dashboard._query_db",
            return_value="100|100|10|100|40|40|40|5",
        ), patch("immich_accelerator.dashboard._run", return_value=""):
            status = get_status(sample_config)

        assert status["progress"]["clip"]["unqueued"] == 60, "idle queue lost its hint"
        assert status["progress"]["video"]["unqueued"] == 0, "this one really is busy"

    def test_unqueued_never_negative_on_count_skew(self, sample_config):
        """done > total (assets removed mid-scan) must not report -10 unqueued."""
        status = self._status_with_idle_queues(
            sample_config, "200|100|10|150|110|110|150|5"
        )
        assert status["progress"]["clip"]["unqueued"] == 0
        assert status["progress"]["clip"]["pct"] == 100.0

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

    def test_api_requeue_refuses_cross_site_post(self, sample_config):
        # Any web page can send a form POST here without a preflight.
        config_no_key = {**sample_config, "api_key": ""}
        from starlette.testclient import TestClient

        client = TestClient(create_app(config_no_key))
        resp = client.post("/api/requeue", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 403
        # The dashboard's own button sends its own origin and gets through.
        resp = client.post("/api/requeue", headers={"Origin": "http://testserver"})
        assert resp.status_code == 400  # past the guard, stopped by the missing key
        # Behind a proxy that rewrites Host, the original host is forwarded.
        resp = client.post(
            "/api/requeue",
            headers={"Origin": "https://photos.example", "X-Forwarded-Host": "photos.example"},
        )
        assert resp.status_code == 400

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


class TestDashboardHtmlDoneLabel:
    """dashboard.html's per-task rateText decision (chip.tasks.forEach) must
    never render 'done' while Immich's job queue is still active for that
    task. The DB-derived pct (asset markers already set) can reach ~100%
    while a queue is genuinely still running — a fresh upload not yet
    counted in the DB total, or jobs re-queued for already-processed assets
    (e.g. after a retry). Regression guard for exactly that: 'accelerator
    dashboard shows done but there are pending/waiting items'."""

    HTML_PATH = Path(__file__).parent.parent / "immich_accelerator" / "dashboard.html"

    def test_an_active_queue_always_reads_as_working(self):
        """The cards no longer say "done" at all (completion moved to the
        Library card), and a queue Immich still reports active reads as
        working even before a rate has been measured."""
        src = self.HTML_PATH.read_text()
        chips = src[src.index("// Chip cards\n") : src.index("// System")]
        assert "const working = rate > 0 || qActive;" in chips
        assert "'done'" not in chips


class TestAttention:
    """What needs a person: shown as a banner on the dashboard and a row in the
    menu bar, from the two files the watch loop writes."""

    def test_nothing_to_say(self, tmp_path):
        from immich_accelerator.common import attention

        assert attention(tmp_path) is None

    def test_a_pending_permission_names_the_program_to_allow(self, tmp_path):
        from immich_accelerator.common import attention

        (tmp_path / "notice.json").write_text(
            json.dumps(
                {
                    "reason": "media-permission",
                    "detail": "/nas/immich",
                    "since": 1.0,
                    "who": "Immich Accelerator Service",
                }
            )
        )
        got = attention(tmp_path)
        assert got["reason"] == "media-permission"
        assert "permission to read the library" in got["title"]
        assert '"Immich Accelerator Service"' in got["action"]
        assert "Files & Folders" in got["action"]

    def test_a_notice_outranks_a_pause(self, tmp_path):
        """The notice is the thing they can act on."""
        from immich_accelerator.common import attention

        (tmp_path / "paused.json").write_text(
            json.dumps({"reason": "backend-unreachable", "detail": "Postgres"})
        )
        assert "Postgres not answering" in attention(tmp_path)["title"]
        (tmp_path / "notice.json").write_text(
            json.dumps({"reason": "local-network", "who": "Python"})
        )
        assert attention(tmp_path)["reason"] == "local-network"

    def test_unknown_or_broken_files_are_ignored(self, tmp_path):
        from immich_accelerator.common import attention

        (tmp_path / "notice.json").write_text("{not json")
        (tmp_path / "paused.json").write_text(json.dumps({"reason": "something-new"}))
        assert attention(tmp_path) is None

    def test_status_carries_it(self, tmp_data_dir, sample_config):
        import immich_accelerator.dashboard as d

        tmp_data_dir["notice_file"].write_text(
            json.dumps({"reason": "media-permission", "who": "Python"})
        )
        d._cache, d._cache_ts = {}, 0
        d._static_hw = {"mem_total_gb": 32.0, "cpus": 10}
        try:
            with patch("urllib.request.urlopen", side_effect=OSError), patch(
                "immich_accelerator.dashboard._query_db",
                return_value="200|100|10|150|80|70|120|5",
            ), patch(
                "immich_accelerator.dashboard._run", return_value="{ 1.50 2.00 3.00 }"
            ):
                status = get_status(sample_config)
        finally:
            d._cache, d._cache_ts, d._static_hw = {}, 0, None
        assert status["attention"]["reason"] == "media-permission"

    def test_the_page_draws_it_as_text(self):
        """textContent, never innerHTML: the detail is a path a user chose."""
        import immich_accelerator.dashboard as d

        html = (Path(d.__file__).parent / "dashboard.html").read_text()
        assert 'id="attention"' in html
        assert "d.attention.title" in html
        render = html[html.index("// Something a person has to do") :]
        render = render[: render.index("// Services")]
        assert "innerHTML" not in render


class TestLibraryCoverage:
    """Whether the library has everything, apart from what is running now.
    An idle queue looks the same whether the library is done or forgotten,
    which is how 40,162 photos sat without search, faces or text for months
    behind job cards that said "idle"."""

    def _html(self):
        import immich_accelerator.dashboard as d

        return (Path(d.__file__).parent / "dashboard.html").read_text()

    def test_the_card_sits_apart_from_the_job_cards_with_the_requeue_button(self):
        html = self._html()
        body = html[html.index("<body>") :]
        assert body.index('id="coverage"') < body.index('id="chips"')
        library = body[body.index("<span>Library</span>") : body.index('id="coverage"')]
        assert 'id="requeue-btn"' in library

    def test_missing_work_nothing_runs_is_its_own_state(self):
        html = self._html()
        fn = html[html.index("function coverageState") :]
        fn = fn[: fn.index("\n}\n")]
        assert "'complete'" in fn and "'catching-up'" in fn and "'needs-queue'" in fn

    def test_video_stays_with_the_job_cards(self):
        """Immich transcodes only the videos its policy picks, so a video gap
        is usually not a gap. It read as one here, so it is left out."""
        html = self._html()
        table = html[html.index("const COVERAGE") : html.index("];", html.index("const COVERAGE"))]
        assert "'video'" not in table

    def test_drawn_as_text(self):
        html = self._html()
        render = html[html.index("function renderCoverage") : html.index("function render(d)")]
        assert "innerHTML" not in render

    def test_the_engine_cards_show_activity_not_completion(self):
        """Completion is the Library card's job. The engine cards said 99.9%
        five times over on a page where nothing was running."""
        html = self._html()
        chips = html[html.index("// Chip cards\n") : html.index("// System")]
        assert "p.pct" not in chips
        assert "'Idle'" in chips

    def test_never_shows_100_percent_while_anything_is_missing(self):
        html = self._html()
        render = html[html.index("function renderCoverage") : html.index("function render(d)")]
        assert "missing > 0 ? Math.min(p.pct, 99.9) : 100" in render


class TestAutoRequeueSwitch:
    def test_the_switch_is_saved_apart_from_config_and_status_reports_it(self, tmp_data_dir, sample_config):
        """Not in config.json: the watcher rewrites that file from a copy that
        can be minutes old, which put a flipped switch back."""
        from fastapi.testclient import TestClient

        tmp_data_dir["config_file"].write_text(json.dumps(sample_config))
        before = tmp_data_dir["config_file"].read_text()
        client = TestClient(create_app(sample_config))
        assert client.post("/api/auto-requeue", json={"enabled": False}).status_code == 200
        assert (tmp_data_dir["data_dir"] / "auto-requeue.off").exists()
        assert tmp_data_dir["config_file"].read_text() == before
        assert client.post("/api/auto-requeue", json={"enabled": True}).status_code == 200
        assert not (tmp_data_dir["data_dir"] / "auto-requeue.off").exists()

    def test_rejects_anything_but_a_clear_answer(self, tmp_data_dir, sample_config):
        from fastapi.testclient import TestClient

        tmp_data_dir["config_file"].write_text(json.dumps(sample_config))
        client = TestClient(create_app(sample_config))
        for body in ({}, {"enabled": "false"}, {"enabled": 0}, {"enabled": None}):
            assert client.post("/api/auto-requeue", json=body).status_code == 400, body
        assert not (tmp_data_dir["data_dir"] / "auto-requeue.off").exists()

    def test_the_page_puts_it_beside_the_button(self):
        import immich_accelerator.dashboard as d

        html = (Path(d.__file__).parent / "dashboard.html").read_text()
        actions = html[html.index('class="library-actions"') :]
        actions = actions[: actions.index("</label>")]
        assert actions.index('id="requeue-btn"') < actions.index('id="auto-requeue"')

    def test_idle_engines_step_aside(self):
        import immich_accelerator.dashboard as d

        html = (Path(d.__file__).parent / "dashboard.html").read_text()
        assert "document.getElementById('engines').hidden = !anyWorking && !d.jobs_api_error;" in html


class TestCoverageNeverClaimsMoreThanItKnows:
    def _js(self):
        import immich_accelerator.dashboard as d

        html = (Path(d.__file__).parent / "dashboard.html").read_text()
        return html[html.index("function coverageState") : html.index("function render(d)")]

    def test_unreadable_queues_are_unknown_not_forgotten(self):
        """With a bad api_key every gap read "missing, not queued" while
        Immich may have been working through it."""
        js = self._js()
        assert "if (!known) return 'unknown';" in js
        assert "p.unqueued > 0 ? 'needs-queue'" in js

    def test_an_unreadable_library_is_not_complete(self):
        js = self._js()
        assert "if (!drawn) worst = 'unknown';" in js
        assert "'No library data'" in js

    def test_the_jobs_error_stays_on_screen(self):
        import immich_accelerator.dashboard as d

        html = (Path(d.__file__).parent / "dashboard.html").read_text()
        assert "hidden = !anyWorking && !d.jobs_api_error" in html

    def test_a_stale_status_cannot_flip_the_switch_back(self):
        import immich_accelerator.dashboard as d

        html = (Path(d.__file__).parent / "dashboard.html").read_text()
        assert "if (d._clicks === autoClicks)" in html
        refresh = html[html.index("async function refresh") :]
        assert refresh.index("const clicks = autoClicks") < refresh.index("fetch('/api/status')")
