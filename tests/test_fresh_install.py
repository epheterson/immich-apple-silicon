"""Fresh-install regression tests.

These are the tests that would have caught issues #17 and #18 before
they shipped in 1.4.0. Both bugs only reproduce on a clean Mac — the
maintainer's machine had globally-installed Python packages and a
corePlugin layer that happened to be large enough to survive the
pre-break. A fresh-install reporter caught them within 14 minutes
of each other.

Every test here simulates an environment the maintainer doesn't have.
"""

from __future__ import annotations

import json
import subprocess
import venv
from pathlib import Path

import pytest

from unittest.mock import patch, MagicMock

import immich_accelerator.__main__ as m
from immich_accelerator.__main__ import (
    SUPPORTED_NODE_MAJORS,
    _COMPOSE_TEMPLATE,
    _STALE_ML_RE,
    _STALE_WORKER_RE,
    _check_node_engines_compat,
    _has_everything,
    _kill_stale_processes,
    _needs_core_plugin,
    _node_major_version,
    _rebuild_sharp,
    _verify_sharp_loads,
    find_node,
)

REPO_ROOT = Path(__file__).parent.parent


# --- Issue #18 — corePlugin extraction break logic ----------------------
#
# Regression: commit f2e4dd2 added a `size_mb < 1` shortcut that broke
# the layer loop BEFORE examining the current layer. Since corePlugin
# lives in a small (~600KB) Docker COPY layer that gets sorted near the
# end of the largest-first order, the break fired right before it was
# extracted. Result: Immich 2.7+ installs missing corePlugin/manifest.json.


class TestNeedsCorePlugin:
    @pytest.mark.parametrize(
        "version,expected",
        [
            ("2.7.0", True),
            ("2.7.1", True),
            ("2.8.0", True),
            ("3.0.0", True),
            ("v2.7.0", True),  # leading 'v'
            ("2.6.3", False),
            ("2.6.0", False),
            ("1.99.99", False),
            ("garbage", True),  # unparseable -> safe default
            ("", True),
        ],
    )
    def test_version_detection(self, version, expected):
        assert _needs_core_plugin(version) == expected


class TestHasEverything:
    """The break-decision function for the OCI layer loop.

    The bug: the old code broke on 'server + build found AND layer < 1MB'
    without first checking whether the CURRENT layer contained corePlugin.
    Since corePlugin is always in a small layer, it was always skipped
    for Immich 2.7+.
    """

    def test_nothing_found_means_keep_going(self):
        assert not _has_everything("2.7.0", False, False, False)
        assert not _has_everything("2.7.0", True, False, False)
        assert not _has_everything("2.7.0", False, True, False)

    def test_modern_immich_requires_core_plugin(self):
        # This is the exact condition that used to short-circuit wrong:
        # server + build extracted, corePlugin NOT yet, small layer coming.
        # The old break said "stop". The correct answer is "keep going".
        assert not _has_everything("2.7.0", True, True, False)
        assert not _has_everything("2.8.5", True, True, False)
        assert not _has_everything("3.0.0", True, True, False)

    def test_modern_immich_stops_when_core_plugin_present(self):
        assert _has_everything("2.7.0", True, True, True)
        assert _has_everything("2.8.5", True, True, True)

    def test_legacy_immich_stops_at_server_and_build(self):
        assert _has_everything("2.6.3", True, True, False)
        assert _has_everything("2.6.3", True, True, True)

    def test_unparseable_version_treated_as_modern(self):
        # Safer to over-fetch one layer than to silently strand corePlugin.
        assert not _has_everything("weird", True, True, False)
        assert _has_everything("weird", True, True, True)

    def test_regression_guards_the_size_shortcut(self):
        """The exact bug: we used to break here. We must NOT break here."""
        # Pretend we just extracted server+build from a big layer and the
        # next layer is 0.3 MB. For Immich 2.7+, that tiny layer might be
        # corePlugin itself — stopping here would strand it.
        found_server = True
        found_build = True
        has_core = False  # haven't processed the current (small) layer yet
        # Any version >= 2.7 must keep going:
        assert not _has_everything("2.7.0", found_server, found_build, has_core)


# --- Issue #17 — dashboard imports must resolve on a fresh install ------
#
# Regression: the Homebrew formula wrapper used the stock python@3.11
# binary, which has no third-party packages on a clean Mac. Dashboard
# imports (fastapi, uvicorn) are lazy, so --version and --help pass
# even though the dashboard subcommand detonates on first use.


class TestDashboardDependenciesAreAvailable:
    """The dashboard needs fastapi + uvicorn. Since the CLI wrapper now
    runs under the ML venv's Python, these MUST stay pinned in
    ml/requirements.txt. If someone removes them, this test fires."""

    def test_fastapi_pinned_in_ml_requirements(self):
        reqs = (REPO_ROOT / "ml" / "requirements.txt").read_text().lower()
        assert "fastapi" in reqs, (
            "fastapi must stay in ml/requirements.txt — the Homebrew "
            "formula wrapper uses the ML venv's Python and the dashboard "
            "imports fastapi lazily. Removing it breaks issue #17."
        )

    def test_uvicorn_pinned_in_ml_requirements(self):
        reqs = (REPO_ROOT / "ml" / "requirements.txt").read_text().lower()
        assert (
            "uvicorn" in reqs
        ), "uvicorn must stay in ml/requirements.txt — see fastapi test above."

    def test_dashboard_module_top_level_imports_only_stdlib(self):
        """Top-level imports of dashboard.py must never reach third-party
        deps — if they did, just importing the module would crash even
        for subcommands that never use the dashboard."""
        import ast

        path = REPO_ROOT / "immich_accelerator" / "dashboard.py"
        tree = ast.parse(path.read_text())
        stdlib_prefixes = {
            "__future__",
            "json",
            "logging",
            "os",
            "subprocess",
            "time",
            "pathlib",
            "urllib",
            "html",
            "importlib",
            "io",
            "tempfile",
            "typing",
            "datetime",
            "collections",
            "functools",
            "itertools",
            "re",
            "socket",
            "sys",
        }
        for node in tree.body:  # top-level only
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in stdlib_prefixes, (
                        f"Top-level import '{alias.name}' in dashboard.py "
                        f"pulls in a third-party dep — move it inside the "
                        f"function that needs it."
                    )
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root in stdlib_prefixes, (
                    f"Top-level 'from {node.module} import ...' in "
                    f"dashboard.py pulls in a third-party dep."
                )


# --- ghcr.io rate-limit retry -------------------------------------------
#
# The first real VM E2E run hit HTTP 429 on a ghcr.io manifest fetch
# during download_immich_server. Anonymous pulls are rate-limited
# per-IP, and a full Immich image fetch involves 20+ requests. A
# single 429 used to fail the whole run. The _get helper now retries
# with exponential backoff.


class TestGhcrRetry:
    """The retry helper is module-level so mocking is trivial. We
    patch `urllib.request.urlopen` and `time.sleep` and exercise the
    helper directly — no need to drive the full download function."""

    def _make_http_error(self, code, headers=None):
        import urllib.error

        return urllib.error.HTTPError(
            url="https://ghcr.io/v2/x/manifests/t",
            code=code,
            msg="err",
            hdrs=headers or {},  # type: ignore[arg-type]
            fp=None,
        )

    def test_retries_429_then_succeeds(self):
        from immich_accelerator.__main__ import _ghcr_urlopen_with_retry

        err_429 = self._make_http_error(429, {"Retry-After": "1"})
        ok_resp = MagicMock(name="ok")

        with (
            patch(
                "urllib.request.urlopen", side_effect=[err_429, ok_resp]
            ) as mock_urlopen,
            patch("time.sleep") as mock_sleep,
        ):
            result = _ghcr_urlopen_with_retry(MagicMock(), timeout=5)

        assert result is ok_resp
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once()
        # Retry-After of "1" flows through verbatim
        assert mock_sleep.call_args[0][0] == 1

    def test_retries_503(self):
        from immich_accelerator.__main__ import _ghcr_urlopen_with_retry

        err_503 = self._make_http_error(503)
        ok_resp = MagicMock()

        with (
            patch("urllib.request.urlopen", side_effect=[err_503, ok_resp]),
            patch("time.sleep"),
        ):
            result = _ghcr_urlopen_with_retry(MagicMock(), timeout=5)
        assert result is ok_resp

    def test_404_is_not_retried(self):
        import urllib.error

        from immich_accelerator.__main__ import _ghcr_urlopen_with_retry

        err_404 = self._make_http_error(404)

        with (
            patch("urllib.request.urlopen", side_effect=err_404),
            patch("time.sleep") as mock_sleep,
        ):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                _ghcr_urlopen_with_retry(MagicMock(), timeout=5)

        assert excinfo.value.code == 404
        mock_sleep.assert_not_called()

    def test_gives_up_after_max_attempts(self):
        import urllib.error

        from immich_accelerator.__main__ import _ghcr_urlopen_with_retry

        err_429 = self._make_http_error(429)

        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[err_429, err_429, err_429, err_429],
            ) as mock_urlopen,
            patch("time.sleep"),
        ):
            with pytest.raises(urllib.error.HTTPError):
                _ghcr_urlopen_with_retry(MagicMock(), timeout=5, max_attempts=4)
        assert mock_urlopen.call_count == 4


# --- Split-setup path-mapping probe (issue #19) -------------------------
#
# Docker stores absolute paths like /data/library/<uuid>/... in Postgres.
# The native worker must write to the same absolute path or the Docker
# API 404s thumbnails. We probe /api/search/metadata at setup time to
# detect Docker's media root and warn if upload_mount diverges.


class TestDetectDockerMediaPrefix:
    """The detector parses an upload-library asset's originalPath to
    recover Docker's IMMICH_MEDIA_LOCATION. Upload-library assets have
    libraryId=null; external-library assets get filtered out because
    their paths don't reflect the upload root.

    v1.4.1 shipped a version that picked external library importPaths
    from /api/libraries, which false-positived on installs with
    external libs plus a correctly-set upload_mount. This test class
    guards against that regression.
    """

    def _patch_urlopen(self, body, raises=None):
        response = MagicMock()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *a: None
        response.read.return_value = body
        if raises:
            return patch("urllib.request.urlopen", side_effect=raises)
        return patch("urllib.request.urlopen", return_value=response)

    def test_extracts_media_root_from_upload_asset(self):
        """Upload-library assets have libraryId=null and the standard
        layout <MEDIA_LOCATION>/upload/<userUUID>/<year>/<filename>."""
        from immich_accelerator.__main__ import _detect_docker_media_prefix

        body = b'{"assets":{"items":[{"libraryId":null,"originalPath":"/data/upload/c37f6663-c090-4262-bcf3-f91a642abcb4/2026/DSC.nef"}]}}'
        with self._patch_urlopen(body):
            result = _detect_docker_media_prefix("http://nas:2283", "fake-key")
        assert result == "/data"

    def test_skips_external_library_assets(self):
        """External-library assets have libraryId set — they must be
        skipped because their paths are library roots, not the
        IMMICH_MEDIA_LOCATION upload root. This is the exact v1.4.1
        regression that false-positived on issue #19's reporter."""
        from immich_accelerator.__main__ import _detect_docker_media_prefix

        body = b'{"assets":{"items":[{"libraryId":"ext-uuid","originalPath":"/external/library/some.jpg"}]}}'
        with self._patch_urlopen(body):
            result = _detect_docker_media_prefix("http://nas:2283", "k")
        assert result is None

    def test_mixed_results_prefers_upload_asset(self):
        """If the response mixes external and upload assets, we find
        and use the upload one (libraryId=null)."""
        from immich_accelerator.__main__ import _detect_docker_media_prefix

        body = (
            b'{"assets":{"items":['
            b'{"libraryId":"ext","originalPath":"/ext/library/a.jpg"},'
            b'{"libraryId":null,"originalPath":"/data/upload/abcdefab-1234-5678-9abc-def012345678/2026/b.jpg"}'
            b"]}}"
        )
        with self._patch_urlopen(body):
            result = _detect_docker_media_prefix("http://nas:2283", "k")
        assert result == "/data"

    def test_returns_none_when_library_is_empty(self):
        """No assets at all -> None (caller treats as 'don't know')."""
        from immich_accelerator.__main__ import _detect_docker_media_prefix

        with self._patch_urlopen(b'{"assets":{"items":[]}}'):
            result = _detect_docker_media_prefix("http://nas:2283", "k")
        assert result is None

    def test_handles_flat_items_response(self):
        """Older Immich versions return a flat items list."""
        from immich_accelerator.__main__ import _detect_docker_media_prefix

        body = b'{"items":[{"libraryId":null,"originalPath":"/data/upload/abcdefab-1234-5678-9abc-def012345678/file.jpg"}]}'
        with self._patch_urlopen(body):
            result = _detect_docker_media_prefix("http://nas:2283", "k")
        assert result == "/data"

    def test_returns_none_without_api_key(self):
        from immich_accelerator.__main__ import _detect_docker_media_prefix

        # No urlopen mock — must not be called because api_key is empty.
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = _detect_docker_media_prefix("http://nas:2283", "")
        assert result is None
        mock_urlopen.assert_not_called()

    def test_returns_none_on_http_error(self):
        import urllib.error

        from immich_accelerator.__main__ import _detect_docker_media_prefix

        err = urllib.error.URLError("unreachable")
        with self._patch_urlopen(b"", raises=err):
            result = _detect_docker_media_prefix("http://down:2283", "k")
        assert result is None


class TestWarnOnPathMismatch:
    def test_no_warning_when_paths_match(self):
        from immich_accelerator.__main__ import _warn_on_path_mismatch

        with patch(
            "immich_accelerator.__main__._detect_docker_media_prefix",
            return_value="/data/library",
        ):
            assert not _warn_on_path_mismatch("http://x", "k", "/data/library")

    def test_no_warning_when_upload_is_parent_of_detected(self):
        """If upload_mount = /data and Docker sees /data/library, the
        worker writes to /data/library correctly — no mismatch."""
        from immich_accelerator.__main__ import _warn_on_path_mismatch

        with patch(
            "immich_accelerator.__main__._detect_docker_media_prefix",
            return_value="/data/library",
        ):
            assert not _warn_on_path_mismatch("http://x", "k", "/data")

    def test_warns_on_real_mismatch(self):
        """Exactly jhoogeboom's case: Docker has /data/library but the
        user's upload_mount is /Volumes/photos."""
        from immich_accelerator.__main__ import _warn_on_path_mismatch

        with patch(
            "immich_accelerator.__main__._detect_docker_media_prefix",
            return_value="/data/library",
        ):
            assert _warn_on_path_mismatch("http://x", "k", "/Volumes/photos")

    def test_no_warning_when_probe_unavailable(self):
        """If we can't determine Docker's prefix, we don't block — we
        just don't know. Caller gets False (no mismatch detected)."""
        from immich_accelerator.__main__ import _warn_on_path_mismatch

        with (
            patch(
                "immich_accelerator.__main__._detect_docker_media_prefix",
                return_value=None,
            ),
            patch(
                "immich_accelerator.__main__._fetch_external_libraries",
                return_value=[],
            ),
        ):
            assert not _warn_on_path_mismatch("http://x", "k", "/anywhere")


class TestRegressionGuards:
    """Static and near-static checks for bugs that got past the VM E2E
    in v1.4.2 — specifically:

      - ORJSONResponse in ml/src/main.py without `orjson` in
        ml/requirements.txt causes every ML request to crash at
        FastAPI's render() with an AssertionError (issue #20).
      - NODE_OPTIONS generated by cmd_start was shell-style quoted
        in v1.4.2, which Node doesn't unquote — the shim path
        ended up containing literal quote characters (issue #24).

    Both regressions fired only at actual execution time — not at
    import, not at config validation. The VM E2E I wrote verified
    imports and config flow but never ran the real execution paths
    where these bugs live. These tests close that gap without
    requiring a full VM spin-up."""

    def test_ml_src_has_no_orjson_response_without_dep(self):
        """If ml/src uses ORJSONResponse, then orjson MUST be in
        ml/requirements.txt. FastAPI's ORJSONResponse.render() does
        `assert orjson is not None` and crashes on every request
        otherwise. This is a pure static check — runs in ms."""
        ml_dir = REPO_ROOT / "ml"
        if not (ml_dir / "src").exists():
            pytest.skip("ml submodule not initialized")

        uses_orjson_response = False
        for py_file in (ml_dir / "src").rglob("*.py"):
            if "ORJSONResponse" in py_file.read_text():
                uses_orjson_response = True
                break

        reqs = (ml_dir / "requirements.txt").read_text().lower()
        has_orjson_dep = "orjson" in reqs

        if uses_orjson_response and not has_orjson_dep:
            pytest.fail(
                "ml/src uses ORJSONResponse but ml/requirements.txt "
                "does not pin orjson. FastAPI's ORJSONResponse.render() "
                "asserts orjson is not None — every /predict will crash. "
                "Either add orjson to requirements or swap to JSONResponse."
            )

    def test_node_options_parseable_by_real_node(self, tmp_path):
        """Simulates exactly what cmd_start does: build a NODE_OPTIONS
        string with --require pointing at a real shim file under a
        path that CONTAINS A SPACE, then spawn node with that env
        and verify the shim loads.

        CRITICAL: the shim is placed under a directory with a space
        in the name so the quoting logic has to actually work. With
        a plain `tmp_path` (no spaces) the v1.4.2 single-quoted bug
        and a v1.4.3 pre-fix backslash-escape variant would both
        pass this test — the whole point of this guard is the
        quoting, so the path MUST contain a space.

        Ground truth (empirically verified, Node 25.2):
            unquoted    → splits on whitespace (fails)
            '…' single  → literals land in filename (v1.4.2 bug)
            \\ backslash → Node does NOT honor shell escapes
            \"…\" double  → WORKS universally
        """
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")

        shim_dir = tmp_path / "dir with spaces"
        shim_dir.mkdir()
        shim = shim_dir / "sentinel_shim.js"
        shim.write_text('process.stderr.write("SHIM_LOADED\\n");\n')
        assert " " in str(shim), "test setup bug: shim path must contain a space"

        # Mimic cmd_start's NODE_OPTIONS construction: double-quote
        # the path. This must match exactly what __main__.py does.
        node_options = f'--require "{shim}"'

        script = tmp_path / "noop.js"
        script.write_text("process.exit(0);\n")
        result = subprocess.run(
            [node, str(script)],
            env={"NODE_OPTIONS": node_options, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            # Generous, because this asserts nothing about speed. The question
            # is whether node parses a quoted --require path containing a
            # space, and a cold node start on a loaded CI runner blew a 10s
            # budget and failed the build for a reason unrelated to what is
            # under test.
            timeout=60,
        )
        if result.returncode != 0:
            pytest.fail(
                f"node failed to load shim via NODE_OPTIONS:\n"
                f"  NODE_OPTIONS={node_options!r}\n"
                f"  exit={result.returncode}\n"
                f"  stdout={result.stdout}\n"
                f"  stderr={result.stderr}"
            )
        assert (
            "SHIM_LOADED" in result.stderr
        ), f"shim did not run despite exit 0. stderr: {result.stderr}"

    def test_node_options_quoted_form_is_broken(self, tmp_path):
        """Negative counterpart: prove the v1.4.2 single-quoted form
        DOES fail with module-not-found when the path contains a
        space. If this ever stops failing, the positive test above
        loses its meaning and the regression guard is invalid."""
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")

        shim_dir = tmp_path / "dir with spaces"
        shim_dir.mkdir()
        shim = shim_dir / "sentinel_shim.js"
        shim.write_text("process.stderr.write('SHIM_LOADED\\n');\n")

        broken = f"--require '{shim}'"
        script = tmp_path / "noop.js"
        script.write_text("process.exit(0);\n")
        result = subprocess.run(
            [node, str(script)],
            env={"NODE_OPTIONS": broken, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, (
            "v1.4.2 quoted form should fail but didn't — " "regression guard invalid"
        )
        assert (
            "Cannot find module" in result.stderr or "MODULE_NOT_FOUND" in result.stderr
        ), f"expected module-not-found error, got: {result.stderr[:300]}"

    def test_cmd_start_node_options_string_is_well_formed(self):
        """Static check that cmd_start wraps the shim path in DOUBLE
        quotes for NODE_OPTIONS. Double is the only form Node's
        NODE_OPTIONS tokenizer honors universally. v1.4.2 shipped
        single quotes (broken — quotes became literal chars in the
        filename). A v1.4.3 pre-fix attempted backslash escaping
        (also broken — Node doesn't honor shell escapes either).
        Verified empirically against Node 25.2."""
        src = (REPO_ROOT / "immich_accelerator" / "__main__.py").read_text()
        # Must wrap the shim path in double quotes.
        assert "f'--require \"{shim_path}\"'" in src, (
            "cmd_start must wrap the shim path in double quotes for "
            "NODE_OPTIONS. See issue #24 and the empirical findings "
            "in TestRegressionGuards."
        )
        # Must not regress to single-quoting the require arg.
        assert "f\"--require '{shim_path}'\"" not in src, (
            "NODE_OPTIONS single-quoted the shim path — Node doesn't "
            "honor shell quoting (v1.4.2 regression, #24)"
        )
        # Must not regress to backslash-escaping whitespace.
        assert 'str(shim_path).replace(" ", r"\\ ")' not in src, (
            "NODE_OPTIONS backslash-escaped whitespace — Node doesn't "
            "honor shell escapes in NODE_OPTIONS either"
        )


class TestPgDumpShim:
    """The JS shim rewrites Immich's hardcoded Linux pg_dump path to
    the Homebrew libpq bin dir at runtime via `--require`. Immich's
    source on disk is never touched — the README's 'unmodified'
    invariant stays true."""

    SHIM_PATH = REPO_ROOT / "immich_accelerator" / "hooks" / "pg_dump_shim.js"

    def test_shim_file_exists(self):
        assert self.SHIM_PATH.exists(), (
            f"hook shim missing: {self.SHIM_PATH}. "
            "cmd_start sets NODE_OPTIONS to require this file; if "
            "it's absent the backup job will still fail with ENOENT."
        )

    def test_shim_is_referenced_by_cmd_start(self):
        """cmd_start must pass the shim to the worker via NODE_OPTIONS.
        Static check against __main__.py so the wiring can't silently
        be removed in a refactor."""
        src = (REPO_ROOT / "immich_accelerator" / "__main__.py").read_text()
        assert "pg_dump_shim.js" in src
        assert "NODE_OPTIONS" in src
        assert "--require" in src

    @pytest.mark.slow
    def test_shim_rewrites_linux_path_via_node_require(self, tmp_path):
        """Real end-to-end check: run node with --require against our
        shim, then call child_process.spawn with the Linux postgres
        path, confirm it rewrites to /opt/homebrew/opt/libpq/bin/.

        Marked slow because it spawns node. Only runs on macOS with
        node installed AND libpq present; skips otherwise."""
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        libpq_bin = Path("/opt/homebrew/opt/libpq/bin/pg_dump")
        if not libpq_bin.exists():
            pytest.skip("libpq not installed — brew install libpq")

        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { spawn } = require('node:child_process');\n"
            "const p = spawn('/usr/lib/postgresql/14/bin/pg_dump', ['--version']);\n"
            "let out = '';\n"
            "p.stdout.on('data', d => out += d);\n"
            "p.on('exit', c => { console.log('exit=' + c + ' out=' + out.trim()); "
            "process.exit(c); });\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"shim rewrite failed:\nstdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "pg_dump (PostgreSQL)" in result.stdout
        # The shim writes its rewrite notice to stderr.
        assert "postgres client interpose" in result.stderr

    # --- gzip --rsyncable regression (issue #24 tail) ---

    @pytest.mark.slow
    def test_shim_rewrites_gzip_rsyncable_to_gnu_gzip(self, tmp_path):
        """Issue #24 final tail: Immich's DatabaseBackupService pipes
        pg_dump output through `gzip --rsyncable`. Apple's BSD gzip
        does NOT support --rsyncable, so the `gzip` child errors out
        immediately and emits zero bytes. Upstream's spawnDuplexStream
        doesn't check gzip's exit code — the pipeline resolves
        "cleanly" and Immich logs 'Database Backup Success' on top
        of a 0-byte file.

        The shim reroutes `gzip --rsyncable` calls to Homebrew's
        GNU gzip (which supports --rsyncable) if installed, and
        falls back to stripping the flag otherwise. This test runs
        the "preferred" path against a real /opt/homebrew/bin/gzip.
        """
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        gnu_gzip = Path("/opt/homebrew/bin/gzip")
        if not gnu_gzip.exists():
            pytest.skip("brew gzip not installed — brew install gzip")

        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { spawnSync } = require('node:child_process');\n"
            "const res = spawnSync('gzip', ['--rsyncable'], {\n"
            "  input: 'hello rsyncable world',\n"
            "});\n"
            "console.log('exit=' + res.status);\n"
            "console.log('bytes=' + res.stdout.length);\n"
            "if (res.status !== 0) { process.exit(1); }\n"
            "if (res.stdout.length === 0) { process.exit(2); }\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"shim gzip rewrite failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "exit=0" in result.stdout
        # Non-zero byte count proves the pipeline actually wrote data,
        # which is the bug-that-was: exit=0 alone can coexist with a
        # zero-byte output file (which is exactly how the silent
        # failure shipped to users).
        assert "bytes=0" not in result.stdout, (
            "shim-rewritten gzip produced 0 bytes — this is the "
            "exact regression that shipped to users in v1.4.2-1.4.5"
        )
        assert "gzip interpose" in result.stderr

    @pytest.mark.slow
    def test_shim_gzip_without_rsyncable_is_untouched(self, tmp_path):
        """Regression guard: if someone calls `gzip` without
        --rsyncable, the shim must not touch the call. We don't want
        to silently reroute every gzip in the worker process — only
        the specific failure case that triggers Immich's backup bug.
        """
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")

        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { spawnSync } = require('node:child_process');\n"
            "const res = spawnSync('gzip', ['-c'], {\n"
            "  input: 'plain gzip call',\n"
            "});\n"
            "console.log('exit=' + res.status);\n"
            "console.log('bytes=' + res.stdout.length);\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        # No interpose message — bare `gzip -c` should pass through.
        assert "gzip interpose" not in result.stderr
        assert "exit=0" in result.stdout

    @pytest.mark.slow
    def test_full_pipeline_produces_valid_gzipped_sql_against_db(self, tmp_path):
        """The integration test Eric asked for: `pg_dump | gzip
        --rsyncable > file`, exactly the shape upstream
        DatabaseBackupService uses, executed through the shim against
        a REAL postgres and verified that the output is (a) non-empty
        and (b) valid gzipped SQL.

        Requires an isolated e2e stack to be up (scripts/e2e-stack.sh
        up). Skips otherwise — we're not going anywhere near prod.
        """
        import gzip as _gzip
        import shutil
        import socket

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        libpq_bin = Path("/opt/homebrew/opt/libpq/bin/pg_dump")
        if not libpq_bin.exists():
            pytest.skip("libpq not installed — brew install libpq")
        # Isolated stack defaults from scripts/e2e-stack.yml
        try:
            with socket.create_connection(("127.0.0.1", 25432), timeout=1):
                pass
        except OSError:
            pytest.skip(
                "isolated e2e stack not running on 127.0.0.1:25432 — "
                "bring it up with scripts/e2e-stack.sh up"
            )

        out = tmp_path / "backup.sql.gz"
        # Reproduce Immich's exact spawn shape inside node + shim.
        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { spawn } = require('node:child_process');\n"
            "const fs = require('fs');\n"
            "const pgdump = spawn('/usr/lib/postgresql/14/bin/pg_dump', [\n"
            "  '--username', 'postgres', '--host', '127.0.0.1',\n"
            "  '--port', '25432', 'immich', '--clean', '--if-exists'\n"
            "], { env: { PATH: process.env.PATH, PGPASSWORD: 'e2epass' } });\n"
            "pgdump.stderr.on('data', c => process.stderr.write('pg: '+c));\n"
            "const gz = spawn('gzip', ['--rsyncable']);\n"
            "gz.stderr.on('data', c => process.stderr.write('gz: '+c));\n"
            f"const out = fs.createWriteStream({str(out)!r});\n"
            "pgdump.stdout.pipe(gz.stdin);\n"
            "gz.stdout.pipe(out);\n"
            "out.on('close', () => { console.log('done'); });\n"
            "out.on('error', e => { console.error('out err', e); "
            "process.exit(1); });\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"backup pipeline failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert out.exists(), "backup file was not created"
        assert out.stat().st_size > 0, (
            "backup file is 0 bytes — the exact regression that "
            "shipped in v1.4.2-1.4.5. Shim routing of `gzip "
            "--rsyncable` is broken."
        )
        # Validate the file is real gzipped SQL.
        with _gzip.open(out, "rt") as fh:
            head = fh.read(4096)
        assert (
            "PostgreSQL database dump" in head
        ), f"backup content doesn't look like a pg_dump:\n{head[:500]}"


class TestExternalLibraryValidation:
    """External-library importPaths must resolve on the Mac filesystem
    or the worker will 404 on those assets. Missing external paths
    are NON-FATAL — they just produce warnings. The worker can still
    process upload and non-missing libraries."""

    def test_missing_external_libs_warn_but_dont_block(self, tmp_path, caplog):
        import logging

        from immich_accelerator.__main__ import _warn_on_path_mismatch

        missing = "/definitely-not-a-real-mount-xyz-test"
        libs = [
            {"name": "NAS Photos", "importPaths": [missing]},
            {"name": "Other", "importPaths": ["/another/missing/path-xyz"]},
        ]
        with (
            patch(
                "immich_accelerator.__main__._detect_docker_media_prefix",
                return_value=None,
            ),
            patch(
                "immich_accelerator.__main__._fetch_external_libraries",
                return_value=libs,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = _warn_on_path_mismatch("http://x", "k", "/data")

        assert result is False, "missing external libs must not block start"
        joined = "\n".join(caplog.messages)
        assert "NAS Photos" in joined
        assert missing in joined
        assert "not accessible" in joined.lower()

    def test_existing_external_libs_produce_no_warning(self, tmp_path, caplog):
        import logging

        from immich_accelerator.__main__ import _warn_on_path_mismatch

        # tmp_path always exists — use it as a library that IS accessible.
        libs = [{"name": "Local", "importPaths": [str(tmp_path)]}]
        with (
            patch(
                "immich_accelerator.__main__._detect_docker_media_prefix",
                return_value=None,
            ),
            patch(
                "immich_accelerator.__main__._fetch_external_libraries",
                return_value=libs,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = _warn_on_path_mismatch("http://x", "k", "/data")

        assert result is False
        joined = "\n".join(caplog.messages)
        assert "not accessible" not in joined.lower()

    def test_upload_mismatch_is_fatal_even_when_external_libs_missing(self, caplog):
        import logging

        from immich_accelerator.__main__ import _warn_on_path_mismatch

        with (
            patch(
                "immich_accelerator.__main__._detect_docker_media_prefix",
                return_value="/real-docker-upload-root",
            ),
            patch(
                "immich_accelerator.__main__._fetch_external_libraries",
                return_value=[
                    {"name": "Missing", "importPaths": ["/does-not-exist-here"]}
                ],
            ),
            caplog.at_level(logging.DEBUG),
        ):
            result = _warn_on_path_mismatch("http://x", "k", "/wrong-mount")

        assert result is True, "upload mismatch is fatal regardless of extlibs"
        joined = "\n".join(caplog.messages)
        assert "Upload path mismatch" in joined
        assert "Missing" in joined  # external warning still appears


# --- Brew-install detection (plist + uninstall safety) -----------------
#
# After the dashboard fix, sys.executable on a brew-installed CLI points
# at libexec/ml/venv/bin/python3.11 under a Cellar-versioned path. If
# setup bakes that path into a launchd plist, the plist goes stale on
# every `brew upgrade`. If uninstall deletes the venv, brew's formula
# becomes half-broken. Both code paths must detect brew installs and
# behave differently.


class TestBrewInstallDetection:
    def test_cellar_path_is_detected_as_brew_install(self):
        # The detection heuristic is a substring match on the resolved
        # __file__. Simulate a Cellar-style path to verify the check.
        brew_path = (
            "/opt/homebrew/Cellar/immich-accelerator/1.4.1/libexec/"
            "immich_accelerator/__main__.py"
        )
        assert "/Cellar/immich-accelerator/" in brew_path

    def test_direct_clone_is_not_detected_as_brew(self):
        direct_path = (
            "/Users/someone/Repos/immich-apple-silicon/"
            "immich_accelerator/__main__.py"
        )
        assert "/Cellar/immich-accelerator/" not in direct_path

    def test_finalize_config_and_uninstall_branch_on_brew_detection(self):
        """Both `_finalize_config` and `cmd_uninstall` must contain the
        brew-install detection guard. This is a static check — if
        someone edits either function and drops the guard, this test
        flags the regression."""
        src = (REPO_ROOT / "immich_accelerator" / "__main__.py").read_text()
        # Both functions set the same `is_brew_install` variable:
        assert src.count('is_brew_install = "/Cellar/immich-accelerator/"') >= 2, (
            "Both _finalize_config and cmd_uninstall must detect brew "
            "installs and avoid touching Cellar-owned files."
        )


class TestKillStaleProcessesPattern:
    """Functional tests for _kill_stale_processes.

    History + post-mortem of the first failed fix:

    v1.0-v1.4.4 used ``pgrep -f "immich|src.main"`` which matched ANY
    command line containing the substring "immich" — including the VM
    E2E harness's `tart run immich-test-run-*` and `docker compose
    ... immich-e2e-stack` subprocesses. Every watchdog tick SIGTERM'd
    them mid-run. We couldn't reproduce the E2E failures until we
    realized it was our own code killing them.

    The FIRST fix attempt used narrower pgrep patterns and validated
    them with Python's ``re.search``. Tests were green. Production
    was still broken because the production call path went through
    ``pgrep -f`` (BSD pgrep on macOS), whose basic-regex flavor
    doesn't understand ``\\s`` or unescaped ``(|)``. The test and
    prod regex engines disagreed — textbook mocking-vs-reality gap.

    Resolution: stop relying on BSD pgrep. Shell out to ``ps`` and
    filter in Python with ``re.compile``. Production and tests now
    share one regex engine. AND these tests now actually EXECUTE
    ``_kill_stale_processes`` with a mocked ``subprocess.run`` that
    returns canned ps output, rather than re-parsing regex strings
    out of the source — the gap that let us ship a broken fix.
    """

    def _ps_output(self, rows):
        """Build a fake `ps -axo pid=,command=` output block.

        `rows` is a list of (pid, cmdline) tuples. Uses BSD ps's
        pid-right-padded layout; production parser is ``split(None,
        1)`` so exact spacing doesn't matter.
        """
        return "\n".join(f"{pid:6d} {cmd}" for pid, cmd in rows) + "\n"

    def _run(self, rows, tracked=None):
        """Invoke _kill_stale_processes against canned ps output.

        Returns the list of (pid, signal) tuples os.kill was called
        on — i.e., the exact set of PIDs production would have
        SIGTERM'd in this world state.
        """
        killed = []
        fake_result = MagicMock()
        fake_result.stdout = self._ps_output(rows)
        with (
            patch(
                "immich_accelerator.__main__.subprocess.run",
                return_value=fake_result,
            ),
            patch(
                "immich_accelerator.__main__.read_pid",
                side_effect=lambda name: (tracked or {}).get(name),
            ),
            patch(
                "immich_accelerator.__main__.os.kill",
                side_effect=lambda pid, sig: killed.append((pid, sig)),
            ),
        ):
            _kill_stale_processes()
        return killed

    # ---- static guard against ever re-adopting the broad pattern ----

    def test_source_does_not_match_bare_immich(self):
        import re as _re

        src = (REPO_ROOT / "immich_accelerator" / "__main__.py").read_text()
        start = src.index("def _kill_stale_processes")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        code_only = _re.sub(r'"""[\s\S]*?"""', "", body)
        code_only = _re.sub(r"^\s*#.*$", "", code_only, flags=_re.M)
        assert (
            '"immich|src.main"' not in code_only
        ), "bare-substring pattern killed the E2E harness; do not revive"
        assert (
            '"immich"' not in code_only
        ), "bare 'immich' substring catches unrelated processes"

    # ---- regex-only sanity checks on the compiled patterns ----

    def test_stale_worker_regex_matches_canonical_worker(self):
        cmd = (
            "/opt/homebrew/opt/node@22/bin/node "
            "/Users/someone/.immich-accelerator/server/2.7.4/dist/main.js"
        )
        assert _STALE_WORKER_RE.search(cmd)

    def test_stale_worker_regex_matches_immich_process_title(self):
        assert _STALE_WORKER_RE.search("immich")
        assert _STALE_WORKER_RE.search("immich ")
        assert not _STALE_WORKER_RE.search("immich-accelerator watch")
        assert not _STALE_WORKER_RE.search("docker compose ... immich-e2e-stack")

    def test_stale_ml_regex_matches_canonical_ml(self):
        cmd = "/Users/someone/.immich-accelerator/ml/venv/bin/python3.11 " "-m src.main"
        assert _STALE_ML_RE.search(cmd)

    def test_stale_ml_regex_rejects_prefix_collision(self):
        # src.maintenance must NOT match. Prefix collision is the
        # whole reason we need an anchor on the pattern.
        cmd = "/opt/homebrew/bin/python3 -m src.maintenance --arg foo"
        assert not _STALE_ML_RE.search(cmd)

    # ---- full _kill_stale_processes functional tests ----

    def test_kills_canonical_worker_and_ml(self):
        rows = [
            (
                1001,
                "/opt/homebrew/opt/node@22/bin/node "
                "/Users/someone/.immich-accelerator/server/2.7.4/dist/main.js",
            ),
            (
                1002,
                "/Users/someone/.immich-accelerator/ml/venv/bin/python3.11 "
                "-m src.main",
            ),
        ]
        killed = self._run(rows)
        killed_pids = {pid for pid, _sig in killed}
        assert 1001 in killed_pids
        assert 1002 in killed_pids
        # Everything should be SIGTERM (15), not SIGKILL
        for _pid, sig in killed:
            assert int(sig) == 15

    def test_skips_tracked_pids(self):
        """Live managed worker PID (tracked in the pidfile) must
        not be SIGTERM'd — that's the job of cmd_stop, not the
        stale-process sweeper."""
        rows = [
            (
                2001,
                "/opt/homebrew/opt/node@22/bin/node "
                "/Users/someone/.immich-accelerator/server/2.7.4/dist/main.js",
            ),
        ]
        killed = self._run(rows, tracked={"worker": 2001})
        assert killed == [], (
            "tracked worker PID 2001 was killed — watchdog is supposed "
            "to leave the live managed process alone"
        )

    def test_does_not_kill_e2e_harness_processes(self):
        """The exact cmdline shapes the old broad pattern was
        killing. All must survive the new sweep."""
        rows = [
            (3001, "tart run --no-graphics immich-test-run-20260415-011735"),
            (
                3002,
                "/Users/someone/.orbstack/bin/docker compose -f "
                "/Users/someone/Repos/immich-apple-silicon/scripts/e2e-stack.yml up -d",
            ),
            (
                3003,
                "socat TCP-LISTEN:12283,bind=192.168.64.1,fork,reuseaddr "
                "TCP:127.0.0.1:22283",
            ),
            (3004, "ssh -i /tmp/iac-e2e-key admin@192.168.64.38"),
            (
                3005,
                "rsync -az /Users/someone/Repos/immich-apple-silicon/"
                "immich_accelerator admin@192.168.64.38:/tmp/iac-src/",
            ),
            (3006, "/opt/homebrew/bin/python3 /tmp/drift_check.py"),
            (3007, "bash scripts/e2e-run.sh"),
            (3008, "vim immich/server/src/main.ts"),
            (3009, "python3 /Users/someone/project/src/main.py"),
        ]
        killed = self._run(rows)
        assert killed == [], f"watchdog killed harness/benign processes: {killed}"

    def test_mixed_kills_only_real_zombies(self):
        """Realistic ps output with both canonical stale processes
        and harness/benign cmdlines. Only the real zombies die."""
        rows = [
            # Real zombies
            (
                4001,
                "/opt/homebrew/opt/node@22/bin/node "
                "/Users/someone/.immich-accelerator/server/2.7.4/dist/main.js",
            ),
            (
                4002,
                "/Users/someone/.immich-accelerator/ml/venv/bin/python3.11 "
                "-m src.main",
            ),
            # Harness + noise — all must survive
            (4101, "tart run --no-graphics immich-test-run-20260415-011735"),
            (4102, "docker compose up immich-e2e-stack"),
            (4103, "/opt/homebrew/bin/python3 -m src.maintenance --flush"),
            (4104, "vim immich/server/src/main.ts"),
        ]
        killed_pids = {pid for pid, _sig in self._run(rows)}
        assert killed_pids == {
            4001,
            4002,
        }, f"expected to kill only {{4001, 4002}}, got {killed_pids}"


class TestNodeVersionPreflight:
    """Regression guards for the Sharp-on-node-25 bug class.

    v1.4.x shipped with `depends_on "node"` in the Homebrew formula
    and `find_node()` falling back to `brew install node`. Both pull
    Homebrew's default node (25.x as of Apr 2026), which breaks
    sharp@0.34.5 native addons with NODE_MODULE_VERSION mismatches.
    The worker crashes mid-Nest-bootstrap at `require('sharp')` with
    a stack trace that looks like an Immich bug.

    These tests lock in the fix so the next regression is caught at
    PR time instead of in the wild.
    """

    def test_supported_majors_includes_22(self):
        # node@22 is the keg-only LTS we depend_on in the formula.
        # If we ever drop 22, the formula must be updated in lockstep.
        assert 22 in SUPPORTED_NODE_MAJORS

    def test_supported_majors_excludes_25(self):
        # The whole point of this module-level constant is to refuse
        # node 25+. If someone accidentally adds it here they've
        # defeated the guard.
        assert 25 not in SUPPORTED_NODE_MAJORS
        assert 26 not in SUPPORTED_NODE_MAJORS

    def test_node_major_version_parses_real_output(self):
        # Empirical — if node isn't installed, skip. On CI the macos
        # runner has it; on dev machines we all have it.
        import shutil as _shutil

        node = _shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        major = _node_major_version(node)
        assert major is not None and major > 0

    def test_node_major_version_handles_missing_binary(self):
        # Nonexistent path — must return None, not raise.
        assert _node_major_version("/nonexistent/node/binary") is None

    def test_check_engines_compat_accepts_supported_node(self, tmp_path):
        # Simulate a package.json with engines.node = 22.x and a
        # fake node binary reporting v22.5.1 via a stub shell script.
        pkg = tmp_path / "package.json"
        pkg.write_text('{"engines":{"node":"22.5.1"}}')
        fake = tmp_path / "node"
        fake.write_text('#!/bin/bash\necho "v22.5.1"\n')
        fake.chmod(0o755)
        ok, msg = _check_node_engines_compat(tmp_path, str(fake))
        assert ok, f"should accept v22 with engines.node=22.5.1, got: {msg}"

    def test_check_engines_compat_rejects_node_25(self, tmp_path):
        # node 25 must be rejected with a message mentioning node@22.
        pkg = tmp_path / "package.json"
        pkg.write_text('{"engines":{"node":"24.14.1"}}')
        fake = tmp_path / "node"
        fake.write_text('#!/bin/bash\necho "v25.9.0"\n')
        fake.chmod(0o755)
        ok, msg = _check_node_engines_compat(tmp_path, str(fake))
        assert not ok, "node 25 must be rejected"
        assert "node@22" in msg, (
            "rejection message must point users at the correct install "
            "command — the whole point of the error is actionability"
        )

    def test_check_engines_compat_missing_package_json_is_ok(self, tmp_path):
        # No package.json (e.g. pre-server-download) — we can't evaluate
        # the constraint, so don't block. The rebuild path catches it.
        fake = tmp_path / "node"
        fake.write_text('#!/bin/bash\necho "v22.5.1"\n')
        fake.chmod(0o755)
        ok, _ = _check_node_engines_compat(tmp_path, str(fake))
        assert ok

    def test_verify_sharp_loads_reports_failure_for_missing_package(self, tmp_path):
        # A cwd with no node_modules — require('sharp') will throw
        # MODULE_NOT_FOUND. The helper must report the failure
        # instead of swallowing it.
        import shutil as _shutil

        node = _shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        ok, err = _verify_sharp_loads(str(tmp_path), node)
        assert not ok
        assert err  # we want actionable stderr back

    def test_formula_template_pins_node_22(self):
        """Static check: the generated Homebrew formula must pin node@22.
        A regression to `depends_on "node"` re-ships the mainline-node bug.
        """
        template = (REPO_ROOT / "scripts" / "render-formula.sh").read_text()
        assert 'depends_on "node@22"' in template, (
            "Formula renderer must pin node@22; "
            'depends_on "node" pulls mainline which breaks sharp.'
        )
        # And the bare version must be GONE, no lingering duplicate.
        assert 'depends_on "node"\n' not in template

    def test_formula_renderer_has_no_backticks(self):
        """The formula is emitted through an UNQUOTED bash heredoc, where a
        backtick or $( ) is command substitution that injects shell output into
        the generated formula and corrupts it (a bug that reached #106 before CI
        caught it). Keep the heredoc body free of both."""
        script = (REPO_ROOT / "scripts" / "render-formula.sh").read_text()
        body = script[script.index("cat >") :]
        assert (
            "`" not in body
        ), "no backticks in the formula heredoc (command substitution)"
        assert "$(" not in body, "no $( ) in the formula heredoc (command substitution)"

    def test_find_node_prefers_node_22_keg(self):
        """find_node must prefer /opt/homebrew/opt/node@22/bin/node
        when it exists. Simulated by patching os.path.isfile.
        """
        with patch("immich_accelerator.__main__.os.path.isfile") as mock_isfile:
            # Only node@22 keg exists.
            def fake_isfile(p):
                return p == "/opt/homebrew/opt/node@22/bin/node"

            mock_isfile.side_effect = fake_isfile
            assert find_node() == "/opt/homebrew/opt/node@22/bin/node"

    def test_rebuild_sharp_raises_when_sharp_missing(self, tmp_path):
        """_rebuild_sharp used to swallow failures and log a warning,
        letting the worker crash opaquely later. The fix makes it raise.
        This test locks in the raise for the trivially-mockable
        "sharp dir doesn't exist" branch.

        We mock find_npm (which transitively calls find_node and
        potentially _brew_install → input()) so the test runs in a
        non-interactive environment without hitting real binaries.
        """
        (tmp_path / "package.json").write_text("{}")
        with patch(
            "immich_accelerator.__main__.find_npm",
            return_value="/usr/bin/false",
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _rebuild_sharp(tmp_path)
        assert "Sharp not found" in str(exc_info.value)
        # Remediation must point at setup, not a dead-end error.
        assert "setup" in str(exc_info.value).lower()

    def test_find_node_rejects_default_node_if_version_unsupported(self):
        """If only /opt/homebrew/bin/node exists and it reports v25,
        find_node must skip it and install node@22. Exercises the
        version-filter, not just the path check.
        """
        with (
            patch("immich_accelerator.__main__.os.path.isfile") as mock_isfile,
            patch("immich_accelerator.__main__._node_major_version") as mock_ver,
            patch("immich_accelerator.__main__._brew_install") as mock_brew,
        ):

            # Only /opt/homebrew/bin/node exists BEFORE brew install,
            # plus the node@22 keg appears AFTER brew install succeeds.
            state = {"after_install": False}

            def fake_isfile(p):
                if p == "/opt/homebrew/bin/node":
                    return True
                if p == "/opt/homebrew/opt/node@22/bin/node":
                    return state["after_install"]
                return False

            def fake_brew_install(pkg):
                assert pkg == "node@22", f"expected brew install node@22, got {pkg}"
                state["after_install"] = True
                return True

            mock_isfile.side_effect = fake_isfile
            mock_ver.return_value = 25  # brew default node is too new
            mock_brew.side_effect = fake_brew_install
            result = find_node()
            assert result == "/opt/homebrew/opt/node@22/bin/node"
            mock_brew.assert_called_once_with("node@22")


@pytest.mark.slow
class TestDashboardStartsInFreshVenv:
    """The canonical repro for #17: build a venv with ONLY the
    dashboard's declared third-party deps, then run dashboard.create_app.

    This simulates exactly what the ML venv provides at runtime. If the
    call succeeds here and ml/requirements.txt lists fastapi+uvicorn,
    the formula wrapper will succeed on a fresh Mac.

    Marked slow because it creates a venv + pip installs.
    """

    def test_create_app_succeeds_with_minimal_deps(self, tmp_path):
        venv_dir = tmp_path / "fresh_venv"
        venv.create(venv_dir, with_pip=True, clear=True)
        pip = venv_dir / "bin" / "pip"
        python = venv_dir / "bin" / "python"

        # Install exactly what ml/requirements.txt ships — the same
        # package composition the Formula pip-installs at post_install.
        # The bare `uvicorn` wheel diverges from `uvicorn[standard]`
        # (uvloop, httptools, websockets, watchfiles, python-dotenv),
        # so we must match the pinned set to make the test a real
        # proxy for "does the shipped formula work?".
        subprocess.run(
            [str(pip), "install", "--quiet", "fastapi", "uvicorn[standard]"],
            check=True,
            timeout=180,
        )

        # Invoke exactly what the wrapper does: run the package with
        # PYTHONPATH pointed at the repo root so our sources resolve.
        result = subprocess.run(
            [
                str(python),
                "-c",
                "from immich_accelerator.dashboard import create_app; "
                "app = create_app({'version':'test','immich_url':'http://x',"
                "'api_key':'','db_hostname':'','db_port':'5432',"
                "'redis_hostname':'','redis_port':'6379',"
                "'server_dir':'/tmp','ml_port':3003}); "
                "print('ok', type(app).__name__)",
            ],
            env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Dashboard create_app failed in fresh venv.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "ok FastAPI" in result.stdout


class TestHeicDecodeShim:
    """The HEIC/RAW decode shim (issues #62, #99) interposes on require('sharp')
    to route HEVC-HEIC paths (by ftyp brand) and camera-RAW paths (by extension)
    through Homebrew libvips. These guard the JS logic without needing real
    Sharp/vips (the real decode is verified on-device)."""

    SHIM = REPO_ROOT / "immich_accelerator" / "hooks" / "heic_decode_shim.js"

    DRIVER = """
const { lazyDecodedSharp } = require(process.argv[2]);
const fake = () => {
    const applied = [];
    const inst = new Proxy({}, {
        get: (_t, p) => (p === 'toBuffer'
            ? async () => applied
            : (...a) => { applied.push(p); return inst; }),
    });
    return inst;
};
(async () => {
    const p = lazyDecodedSharp('/nonexistent/x.arw', {}, fake);
    const a = p.rotate(90);
    const b = p.clone().resize(100);
    const out = { a: await a.toBuffer(), b: await b.toBuffer() };
    try {
        lazyDecodedSharp('/nonexistent/y.arw', {}, fake).pipe(process.stdout);
        out.threw = '';
    } catch (e) { out.threw = e.message; }
    console.log('RESULT' + JSON.stringify(out));
})();
"""

    def _drive(self, tmp_path):
        """Run the proxy against a fake Sharp. The input is a RAW path that does
        not exist, so the decode fails and falls through to the original input;
        what is under test is the recorded chain, not the decode."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        driver = tmp_path / "drive.js"
        driver.write_text(self.DRIVER)
        proc = subprocess.run(
            [node, str(driver), str(self.SHIM)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        line = [x for x in proc.stdout.splitlines() if x.startswith("RESULT")]
        assert line, f"driver produced no result:\n{proc.stdout}\n{proc.stderr}"
        return json.loads(line[0][len("RESULT") :])

    def test_clone_forks_the_chain_instead_of_sharing_it(self, tmp_path):
        """clone() exists to send one pipeline to two outputs. Recorded as an
        ordinary chain call it returned the same proxy, so both branches wrote
        to one list and each output silently got the other's operations."""
        out = self._drive(tmp_path)
        assert out["a"] == ["rotate"], "the original must not gain the fork's calls"
        assert out["b"] == ["rotate", "resize"], "the fork inherits, then diverges"

    def test_stream_use_fails_by_name(self, tmp_path):
        """A Sharp instance is also a stream; this proxy cannot be one, because
        the decode it stands in for is async. Immich 3.0.2 never streams on this
        path, so this is about the version after that failing somewhere it can
        be recognised."""
        out = self._drive(tmp_path)
        assert "immich-accelerator" in out["threw"]
        assert "pipe()" in out["threw"]

    def test_shim_file_exists(self):
        assert self.SHIM.exists(), "heic_decode_shim.js must ship in hooks/"

    def test_shim_is_wired_into_worker_node_options(self):
        """cmd_start must add the shim to the worker's NODE_OPTIONS."""
        src = (REPO_ROOT / "immich_accelerator" / "__main__.py").read_text()
        assert "heic_decode_shim.js" in src
        assert 'require "{heic_shim}"' in src or "--require" in src

    def test_shim_syntax_valid(self):
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        result = subprocess.run(
            [node, "--check", str(self.SHIM)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"shim syntax error: {result.stderr}"

    # --- functional decoder-chain tests (fake sharp + stub decoders) ---
    _HEIC_BYTES = bytes([0, 0, 0, 0x18]) + b"ftypheic" + b"\x00\x00\x00\x00mif1heic"

    def _fake_sharp(self, tmp_path):
        sharp_dir = tmp_path / "node_modules" / "sharp"
        sharp_dir.mkdir(parents=True)
        (sharp_dir / "index.js").write_text(
            "function fakeSharp(input){"
            "process.stdout.write('INPUT_TYPE:'+"
            "(Buffer.isBuffer(input)?'buffer':typeof input)+'\\n');"
            # A real Sharp instance is only usable via its (async) output
            # methods; the shim's lazy proxy defers the decode until one of
            # those is called. metadata() here stands in for that terminal
            # call so the driver exercises the real chain-then-await shape.
            "return {metadata: async () => ({})};}"
            "fakeSharp.cache=function(){};"
            "module.exports=fakeSharp;\n"
        )

    def _stub(self, path_, body):
        # Decoder stubs: the output path is always the last argument (vips
        # `autorot in out[compression=deflate]`, sips `... --out out`). vips
        # accepts a trailing `[options]` suffix on the save target and writes to
        # the base path; strip it (`[[]` is a glob for a literal `[`) so the stub
        # writes where the shim reads back.
        path_.write_text('#!/bin/bash\nout="${@: -1}"\nout="${out%%[[]*}"\n' + body)
        path_.chmod(0o755)

    # A minimal little-endian TIFF header (II*\0) — enough for the shim's isTiff
    # gate; the fake Sharp only checks Buffer-vs-string, not pixels.
    _TIFF = r'printf "II*\000STUBTIFFDATA" > "$out"' + "\n"

    def _run(self, tmp_path, node, env_extra, argv):
        driver = tmp_path / "driver.js"
        # Decode is async now, so sharp(path) alone starts it but doesn't wait
        # for it. Await metadata() per file, sequentially, so INPUT_TYPE lines
        # land in a deterministic order regardless of decode latency — this is
        # also exactly the chain-then-await shape Immich's own code uses.
        awaits = "".join(
            f"await sharp(process.argv[{i + 2}]).metadata();" for i in range(len(argv))
        )
        driver.write_text(
            "const sharp=require('sharp');"
            "process.stdout.write('WRAPPED:'+(sharp.__heicShimWrapped===true)+'\\n');"
            f"(async () => {{{awaits}}})().catch(e => {{ console.error(e); process.exit(1); }});"
            "\n"
        )
        env = {"PATH": "/usr/bin:/bin", **env_extra}
        return subprocess.run(
            [node, "--require", str(self.SHIM), str(driver), *argv],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def _node_or_skip(self):
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        return node

    def test_a_second_output_does_not_re_apply_the_chain(self, tmp_path):
        """Two outputs from one pipeline must each get the chain once.

        The lazy proxy records chain calls and replays them when a terminal
        method runs. Replaying onto a single shared Sharp instance meant the
        second output got rotate/resize applied on top of the first, because
        real chain methods return `this`. Immich 3.0.2 does not reuse a
        pipeline, so nothing triggers it today, but this shim wraps whichever
        Immich the user is running and the failure is silent: wrong pixels, no
        error. Each terminal builds its own instance now.
        """
        node = self._node_or_skip()
        # A fake Sharp that records what was applied to each instance it makes.
        (tmp_path / "node_modules" / "sharp").mkdir(parents=True, exist_ok=True)
        (tmp_path / "node_modules" / "sharp" / "index.js").write_text(
            "const instances=[];"
            "function fakeSharp(){const mine=[];instances.push(mine);"
            "const i={rotate(){mine.push('rotate');return i;},"
            "resize(){mine.push('resize');return i;},"
            "async toBuffer(){return Buffer.from('x');},"
            "async metadata(){return {};}};return i;}"
            "fakeSharp.cache=function(){};fakeSharp.__instances=instances;"
            "module.exports=fakeSharp;\n"
        )
        driver = tmp_path / "driver.js"
        # A .arw path routes into the lazy proxy on a pure string check, with
        # no file needed; the decode fails and falls back, which is fine here.
        driver.write_text(
            "const sharp=require('sharp');"
            "(async () => {"
            "  const p = sharp('/tmp/nonexistent-chain-probe.arw');"
            "  await p.rotate(90).resize(100).toBuffer();"
            "  await p.toBuffer();"
            "  const per = sharp.__instances.map(i => i.join('+'));"
            "  process.stdout.write('PER:'+JSON.stringify(per)+'\\n');"
            "})().catch(e => { console.error(e); process.exit(1); });\n"
        )
        r = subprocess.run(
            [node, "--require", str(self.SHIM), str(driver)],
            cwd=str(tmp_path),
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert r.returncode == 0, r.stderr
        line = next(l for l in r.stdout.splitlines() if l.startswith("PER:"))
        applied = json.loads(line[len("PER:") :])
        assert applied, "the proxy never built a Sharp instance"
        for chain in applied:
            assert (
                chain.count("rotate") <= 1
            ), f"chain applied more than once to one instance: {applied}"

    def test_routes_heic_to_vips_and_passes_others_through(self, tmp_path):
        """HEIC path is decoded to a Buffer via the primary (vips) decoder; a
        non-HEIC path passes through untouched as a string."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        vips = tmp_path / "vips_stub.sh"
        self._stub(vips, self._TIFF)
        heic = tmp_path / "photo.heic"
        heic.write_bytes(self._HEIC_BYTES)
        notheic = tmp_path / "plain.jpg"
        notheic.write_bytes(b"\xff\xd8\xff\xe0not-really-but-not-ftyp")

        result = self._run(
            tmp_path,
            node,
            {"IMMICH_ACCELERATOR_VIPS": str(vips)},
            [str(heic), str(notheic)],
        )
        assert result.returncode == 0, f"driver failed: {result.stderr}"
        assert "WRAPPED:true" in result.stdout.splitlines()
        types = [
            ln for ln in result.stdout.splitlines() if ln.startswith("INPUT_TYPE:")
        ]
        assert types == ["INPUT_TYPE:buffer", "INPUT_TYPE:string"], result.stdout

    def test_prefers_vips_over_sips(self, tmp_path):
        """vips (libheif, headless) is tried first; when it succeeds sips is
        never touched — that is what makes HEIC work without a GUI session."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        vips_used = tmp_path / "vips_used"
        sips_used = tmp_path / "sips_used"
        vips = tmp_path / "vips_stub.sh"
        sips = tmp_path / "sips_stub.sh"
        self._stub(vips, f"touch {vips_used}\n" + self._TIFF)
        self._stub(sips, f"touch {sips_used}\n" + self._TIFF)
        heic = tmp_path / "photo.heic"
        heic.write_bytes(self._HEIC_BYTES)

        result = self._run(
            tmp_path,
            node,
            {
                "IMMICH_ACCELERATOR_VIPS": str(vips),
                "IMMICH_ACCELERATOR_SIPS": str(sips),
            },
            [str(heic)],
        )
        assert result.returncode == 0, f"driver failed: {result.stderr}"
        assert "INPUT_TYPE:buffer" in result.stdout
        assert vips_used.exists(), "vips (primary) should have run"
        assert not sips_used.exists(), "sips must not run when vips succeeds"

    def test_falls_through_to_sips_when_vips_produces_empty(self, tmp_path):
        """The whole point of the rewrite: a decoder that exits 0 but writes an
        empty/non-TIFF file (a GUI-less sips, or here a stubbed vips) must be
        rejected so the next decoder gets a turn."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        sips_used = tmp_path / "sips_used"
        vips = tmp_path / "vips_stub.sh"
        sips = tmp_path / "sips_stub.sh"
        self._stub(vips, ': > "$out"\n')  # exit 0, empty output
        self._stub(sips, f"touch {sips_used}\n" + self._TIFF)
        heic = tmp_path / "photo.heic"
        heic.write_bytes(self._HEIC_BYTES)

        result = self._run(
            tmp_path,
            node,
            {
                "IMMICH_ACCELERATOR_VIPS": str(vips),
                "IMMICH_ACCELERATOR_SIPS": str(sips),
            },
            [str(heic)],
        )
        assert result.returncode == 0, f"driver failed: {result.stderr}"
        assert "INPUT_TYPE:buffer" in result.stdout, result.stdout
        assert sips_used.exists(), "must fall through to sips when vips output is empty"

    def test_routes_raw_by_extension_and_leaves_ordinary_tiff_alone(self, tmp_path):
        """Camera RAW is matched by EXTENSION (issue #99): a .cr2/.nef path is
        decoded to a Buffer via vips, while an ordinary .tif (which Sharp handles
        natively) passes through untouched as a string. Guards against the
        extension set over-matching plain TIFFs."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        vips = tmp_path / "vips_stub.sh"
        self._stub(vips, self._TIFF)
        cr2 = tmp_path / "photo.cr2"
        cr2.write_bytes(b"content-irrelevant-extension-is-what-matters")
        nef = tmp_path / "photo.nef"
        nef.write_bytes(b"II*\x00whatever")
        plain_tif = tmp_path / "scan.tif"
        plain_tif.write_bytes(b"II*\x00ordinary-tiff-sharp-handles-this")

        result = self._run(
            tmp_path,
            node,
            {"IMMICH_ACCELERATOR_VIPS": str(vips)},
            [str(cr2), str(nef), str(plain_tif)],
        )
        assert result.returncode == 0, f"driver failed: {result.stderr}"
        types = [
            ln for ln in result.stdout.splitlines() if ln.startswith("INPUT_TYPE:")
        ]
        assert types == [
            "INPUT_TYPE:buffer",
            "INPUT_TYPE:buffer",
            "INPUT_TYPE:string",
        ], result.stdout

    def test_strips_nonexistent_vipshome_from_decoder_env(self, tmp_path):
        """Sharp's prebuilt libvips exports a bogus VIPSHOME (its GitHub-runner
        build path) into the environment. If the spawned Homebrew vips inherits
        it, vips looks for its loader modules there, fails ("not a known file
        format"), and decode silently falls to sips (GUI-only). The shim drops a
        VIPSHOME pointing at a nonexistent dir but keeps a real one."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        marker = tmp_path / "vipshome_seen"
        vips = tmp_path / "vips_stub.sh"
        self._stub(
            vips, 'echo "[${VIPSHOME-UNSET}]" > ' + str(marker) + "\n" + self._TIFF
        )
        heic = tmp_path / "photo.heic"
        heic.write_bytes(self._HEIC_BYTES)

        bogus = tmp_path / "does-not-exist"
        r = self._run(
            tmp_path,
            node,
            {"IMMICH_ACCELERATOR_VIPS": str(vips), "VIPSHOME": str(bogus)},
            [str(heic)],
        )
        assert r.returncode == 0, r.stderr
        assert (
            marker.read_text().strip() == "[UNSET]"
        ), "nonexistent VIPSHOME must be stripped"

        real = tmp_path / "real-vipshome"
        real.mkdir()
        r2 = self._run(
            tmp_path,
            node,
            {"IMMICH_ACCELERATOR_VIPS": str(vips), "VIPSHOME": str(real)},
            [str(heic)],
        )
        assert r2.returncode == 0, r2.stderr
        assert (
            marker.read_text().strip() == "[" + str(real) + "]"
        ), "existing VIPSHOME must be preserved"

    # --- async decode: lock-renewal-starvation regression + concurrency ---

    def test_chain_calls_are_replayed_onto_real_sharp_in_order(self, tmp_path):
        """Immich chains synchronous pipeline calls (rotate, resize, ...)
        before awaiting a terminal method (toBuffer/toFile/metadata). Since
        the decode is now async, sharp() can't call the real Sharp
        synchronously anymore — it must record those chained calls and
        replay them, in order and with their original arguments, onto the
        real instance once decode resolves. This is the mechanism that lets
        the decode run fully async while still looking like an ordinary
        chainable Sharp pipeline to callers."""
        node = self._node_or_skip()
        sharp_dir = tmp_path / "node_modules" / "sharp"
        sharp_dir.mkdir(parents=True)
        (sharp_dir / "index.js").write_text(
            "function fakeSharp(input){"
            "const calls=[];"
            "const inst={"
            "rotate:(...a)=>{calls.push('rotate('+JSON.stringify(a)+')');return inst;},"
            "resize:(...a)=>{calls.push('resize('+JSON.stringify(a)+')');return inst;},"
            "toBuffer:async()=>{process.stdout.write('CALLS:'+calls.join(',')+'\\n');return Buffer.from('x');},"
            "};"
            "return inst;}"
            "fakeSharp.cache=function(){};"
            "module.exports=fakeSharp;\n"
        )
        vips = tmp_path / "vips_stub.sh"
        self._stub(vips, self._TIFF)
        heic = tmp_path / "photo.heic"
        heic.write_bytes(self._HEIC_BYTES)
        driver = tmp_path / "driver.js"
        driver.write_text(
            "const sharp=require('sharp');"
            "sharp(process.argv[2]).rotate(90).resize(200,200).toBuffer()"
            ".catch(e=>{console.error(e);process.exit(1);});\n"
        )
        env = {"PATH": "/usr/bin:/bin", "IMMICH_ACCELERATOR_VIPS": str(vips)}
        result = subprocess.run(
            [node, "--require", str(self.SHIM), str(driver), str(heic)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert "CALLS:rotate([90]),resize([200,200])" in result.stdout, result.stdout

    def test_decode_does_not_block_the_event_loop(self, tmp_path):
        """The bug this fix addresses: a synchronous (execFileSync) decode
        used to block Node's entire event loop — and with it BullMQ's
        lock-renewal timer — for the decode's full duration
        (#could-not-renew-lock). Prove the event loop keeps ticking during a
        slow decode by racing a timer against it; with the old synchronous
        decode this would report ~0 ticks."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        vips = tmp_path / "vips_stub.sh"
        self._stub(vips, "sleep 0.3\n" + self._TIFF)
        heic = tmp_path / "photo.heic"
        heic.write_bytes(self._HEIC_BYTES)
        driver = tmp_path / "driver.js"
        driver.write_text(
            "const sharp=require('sharp');"
            "let ticks=0;"
            "const timer=setInterval(()=>{ticks++;},20);"
            "sharp(process.argv[2]).metadata().then(()=>{"
            "clearInterval(timer);"
            "process.stdout.write('TICKS:'+ticks+'\\n');"
            "}).catch(e=>{console.error(e);process.exit(1);});\n"
        )
        env = {"PATH": "/usr/bin:/bin", "IMMICH_ACCELERATOR_VIPS": str(vips)}
        result = subprocess.run(
            [node, "--require", str(self.SHIM), str(driver), str(heic)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        ticks = int(result.stdout.split("TICKS:")[1].strip())
        assert ticks >= 5, (
            f"event loop was blocked during decode (only {ticks} timer "
            f"ticks in ~300ms of decode time); output: {result.stdout}"
        )

    def _run_two_concurrently(self, tmp_path, node, vips, env_extra):
        heic1 = tmp_path / "a.heic"
        heic1.write_bytes(self._HEIC_BYTES)
        heic2 = tmp_path / "b.heic"
        heic2.write_bytes(self._HEIC_BYTES)
        driver = tmp_path / "driver.js"
        driver.write_text(
            "const sharp=require('sharp');"
            "Promise.all([sharp(process.argv[2]).metadata(),sharp(process.argv[3]).metadata()])"
            ".then(()=>process.stdout.write('DONE\\n'))"
            ".catch(e=>{console.error(e);process.exit(1);});\n"
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "IMMICH_ACCELERATOR_VIPS": str(vips),
            **env_extra,
        }
        return subprocess.run(
            [node, "--require", str(self.SHIM), str(driver), str(heic1), str(heic2)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_decode_concurrency_defaults_to_serialized(self, tmp_path):
        """Async execFile let BullMQ's job concurrency fire multiple
        concurrent decodes at once; measured on a real NAS/SMB share, that
        made things SLOWER, not faster (a modest NAS degrades under
        concurrent reads rather than parallelizing them). Default
        concurrency is 1 — verify two decodes issued at the same time never
        actually overlap."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        lock_dir = tmp_path / "running.lock"
        violations = tmp_path / "violations"
        vips = tmp_path / "vips_stub.sh"
        self._stub(
            vips,
            f'if ! mkdir "{lock_dir}" 2>/dev/null; then echo overlap >> "{violations}"; fi\n'
            "sleep 0.15\n"
            f'rmdir "{lock_dir}"\n' + self._TIFF,
        )
        result = self._run_two_concurrently(tmp_path, node, vips, {})
        assert result.returncode == 0, result.stderr
        assert "DONE" in result.stdout
        assert (
            not violations.exists()
        ), "two decodes ran concurrently despite the default concurrency of 1"

    def test_decode_concurrency_env_override_allows_parallel_decodes(self, tmp_path):
        """IMMICH_ACCELERATOR_HEIC_DECODE_CONCURRENCY raises the limit for an
        operator on storage that can actually take concurrent reads —
        confirm it isn't a no-op by proving decodes DO overlap once raised."""
        node = self._node_or_skip()
        self._fake_sharp(tmp_path)
        lock_dir = tmp_path / "running.lock"
        overlapped = tmp_path / "overlapped"
        vips = tmp_path / "vips_stub.sh"
        self._stub(
            vips,
            f'if ! mkdir "{lock_dir}" 2>/dev/null; then touch "{overlapped}"; fi\n'
            "sleep 0.15\n"
            f'rmdir "{lock_dir}" 2>/dev/null\n' + self._TIFF,
        )
        result = self._run_two_concurrently(
            tmp_path, node, vips, {"IMMICH_ACCELERATOR_HEIC_DECODE_CONCURRENCY": "2"}
        )
        assert result.returncode == 0, result.stderr
        assert "DONE" in result.stdout
        assert (
            overlapped.exists()
        ), "raising concurrency to 2 should let two decodes run at once"


class TestHardwareEncodingCanBeTurnedOff:
    """Hardware encoding is a choice, not a law.

    On an idle Mac the software encoder often finishes one file sooner, because
    Immich asks for preset ultrafast; what the hardware buys is the machine,
    roughly one core against every core software can reach. Which of those a
    person wants depends on their Mac and what else it is doing, so there has to
    be a way to say. Asked for in #155.
    """

    WRAPPER = REPO_ROOT / "immich_accelerator" / "ffmpeg-wrapper.sh"

    def _run(self, tmp_path, args, env=None):
        import os

        echo = tmp_path / "ffmpeg"
        echo.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
        echo.chmod(0o755)
        w = tmp_path / "w.sh"
        w.write_text(
            self.WRAPPER.read_text().replace(
                'REAL_FFMPEG="/opt/homebrew/bin/ffmpeg"', f'REAL_FFMPEG="{echo}"'
            )
        )
        w.chmod(0o755)
        r = subprocess.run(
            ["/bin/bash", str(w), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        )
        return r.stdout.split("\n")

    def test_hardware_is_the_default(self, tmp_path):
        out = self._run(tmp_path, ["-i", "in.mov", "-c:v", "h264", "out.mp4"])
        assert "h264_videotoolbox" in out

    def test_it_can_be_turned_off(self, tmp_path):
        out = self._run(
            tmp_path,
            ["-i", "in.mov", "-c:v", "h264", "out.mp4"],
            env={"IMMICH_ACCEL_HW_VIDEO": "0"},
        )
        assert "h264" in out and "h264_videotoolbox" not in out

    def test_hevc_too(self, tmp_path):
        out = self._run(
            tmp_path,
            ["-i", "in.mov", "-c:v", "hevc", "out.mp4"],
            env={"IMMICH_ACCEL_HW_VIDEO": "0"},
        )
        assert "hevc" in out and "hevc_videotoolbox" not in out

    def test_the_words_people_actually_type_all_work(self, tmp_path):
        for value in ("0", "false", "no"):
            out = self._run(
                tmp_path,
                ["-i", "in.mov", "-c:v", "h264", "out.mp4"],
                env={"IMMICH_ACCEL_HW_VIDEO": value},
            )
            assert "h264_videotoolbox" not in out, f"{value!r} should turn it off"

    def test_anything_else_leaves_it_on(self, tmp_path):
        """An unset or unrecognised value must not silently disable the
        hardware path on every install."""
        for value in ("1", "true", "yes", ""):
            out = self._run(
                tmp_path,
                ["-i", "in.mov", "-c:v", "h264", "out.mp4"],
                env={"IMMICH_ACCEL_HW_VIDEO": value},
            )
            assert "h264_videotoolbox" in out, f"{value!r} should leave it on"

    def test_hevc_still_gets_the_hvc1_tag_with_the_switch_off(self, tmp_path):
        """hev1 (ffmpeg's default) stores parameter sets in-band and Apple's
        decoder rejects it, so the wrapper injects hvc1 when the caller did not.
        That is a property of the output container rather than of the encoder,
        and libx265 defaults to hev1 as well."""
        out = self._run(
            tmp_path,
            ["-i", "in.mov", "-c:v", "hevc", "out.mp4"],
            env={"IMMICH_ACCEL_HW_VIDEO": "0"},
        )
        assert "hevc_videotoolbox" not in out, "the switch must still be honoured"
        assert "hvc1" in out

    def test_a_tag_the_caller_passed_is_still_left_alone(self, tmp_path):
        """Immich passes -tag:v hvc1 itself for an HEVC target, so the injection
        is a fallback and must not add a second tag."""
        out = self._run(
            tmp_path,
            ["-i", "in.mov", "-c:v", "hevc", "-tag:v", "hvc1", "out.mp4"],
            env={"IMMICH_ACCEL_HW_VIDEO": "0"},
        )
        assert out.count("hvc1") == 1, out


class TestEncodingSwitches:
    """The `encoding` command, which is what the Settings switch calls.

    The app shells out to this rather than writing config.json itself, so there
    is one implementation of what a switch means and one place to test it.
    """

    def _args(self, switch=None, state=None):
        import argparse

        return argparse.Namespace(switch=switch, state=state)

    def test_switches_default_to_on(self, tmp_data_dir):
        m.save_config({})
        assert m.encoding_switch_on("hardware-video") is True

    def test_turning_one_off_persists_to_config(self, tmp_data_dir):
        m.save_config({})
        m.cmd_encoding(self._args("hardware-video", "off"))
        assert m.load_config()["env"]["IMMICH_ACCEL_HW_VIDEO"] == "0"
        assert m.encoding_switch_on("hardware-video") is False

    def test_turning_it_back_on_persists_too(self, tmp_data_dir):
        """An off switch must be re-settable. Deleting the key would also read
        as on, but leaves no record that anyone chose it."""
        m.save_config({"env": {"IMMICH_ACCEL_HW_VIDEO": "0"}})
        m.cmd_encoding(self._args("hardware-video", "on"))
        assert m.encoding_switch_on("hardware-video") is True

    def test_it_keeps_the_rest_of_the_config(self, tmp_data_dir):
        """Writing a switch must not drop settings, which is how a config
        rewrite quietly destroys an install."""
        m.save_config({"immich_url": "http://10.0.0.9:2283", "api_key": "abc"})
        m.cmd_encoding(self._args("hardware-video", "off"))
        after = m.load_config()
        assert after["immich_url"] == "http://10.0.0.9:2283"
        assert after["api_key"] == "abc"

    def test_it_keeps_other_env_entries(self, tmp_data_dir):
        m.save_config({"env": {"IMMICH_ACCEL_CONCURRENCY": "3"}})
        m.cmd_encoding(self._args("hardware-video", "off"))
        env = m.load_config()["env"]
        assert env["IMMICH_ACCEL_CONCURRENCY"] == "3"
        assert env["IMMICH_ACCEL_HW_VIDEO"] == "0"

    def test_a_real_environment_variable_still_wins(self, tmp_data_dir, monkeypatch):
        """Someone who exported one is debugging, and a file must not overrule
        them. Same precedence as int_setting."""
        m.save_config({"env": {"IMMICH_ACCEL_HW_VIDEO": "0"}})
        monkeypatch.setenv("IMMICH_ACCEL_HW_VIDEO", "1")
        assert m.encoding_switch_on("hardware-video") is True

    def test_listing_does_not_write_anything(self, tmp_data_dir):
        m.save_config({"immich_url": "http://x"})
        before = m.load_config()
        m.cmd_encoding(self._args())
        assert m.load_config() == before

    def test_an_install_from_before_these_settings_reads_as_custom(self, tmp_data_dir):
        """Hardware video without hardware audio is genuinely neither end, and
        saying so is better than pretending. One click moves it to an end."""
        m.save_config({})
        assert m.encoding_preset() == "custom"

    def test_every_preset_round_trips(self, tmp_data_dir):
        """Applying a preset must make encoding_preset name that same preset.
        Derived state and written state disagreeing is how a UI ends up
        showing 'custom' immediately after you picked something."""
        for name in m.ENCODING_PRESETS:
            config = m.apply_encoding_preset(name, {})
            assert m.encoding_preset(config) == name, name

    def test_stock_turns_everything_off(self, tmp_data_dir):
        """Stock is the pass-through position. Any hardware switch left on
        means output that is not what Docker would have produced."""
        config = m.apply_encoding_preset("stock", {})
        for var in m.ENCODING_PRESETS["stock"]:
            assert config["env"][var] == "0", var

    def test_a_preset_states_every_switch(self, tmp_data_dir):
        """A preset that only wrote the switches it cared about would leave a
        hand-set switch in place under a name that denies it."""
        for name, wanted in m.ENCODING_PRESETS.items():
            assert set(wanted) == {
                var for var, _ in m.ENCODING_SWITCHES.values()
            }, f"{name} does not name every switch"

    def test_switching_by_hand_reports_custom(self, tmp_data_dir):
        m.save_config(m.apply_encoding_preset("stock", {}))
        m.cmd_encoding(self._args("hardware-video", "on"))
        assert m.encoding_preset() == "custom"

    def test_a_preset_recovers_from_custom(self, tmp_data_dir):
        m.save_config(m.apply_encoding_preset("stock", {}))
        m.cmd_encoding(self._args("hardware-video", "on"))
        m.save_config(m.apply_encoding_preset("stock", m.load_config()))
        assert m.encoding_preset() == "stock"

    def test_the_audio_switch_defaults_off(self, tmp_data_dir):
        """It changes output, so it only happens where it was asked for."""
        m.save_config({})
        assert m.encoding_switch_on("hardware-audio") is False

    def test_stock_selects_the_engine_that_can_actually_do_it(self, tmp_data_dir):
        """The whole failure this guards against: Stock writes the ffmpeg
        switches, leaves the native engine in place, and the label claims
        Docker-identical output while faces, text and search are still Apple
        Vision and mlx."""
        config = m.apply_encoding_preset("stock", {})
        assert config["ml_engine"] == "python"
        assert config["stock_ml"] is True

    def test_the_apple_silicon_end_keeps_the_native_engine(self, tmp_data_dir):
        config = m.apply_encoding_preset("apple-silicon", {})
        assert config["ml_engine"] == "native"
        assert config["stock_ml"] is False

    def test_turning_off_one_switch_reads_as_custom(self, tmp_data_dir):
        m.save_config(m.apply_encoding_preset("apple-silicon", {}))
        m.cmd_encoding(self._args("hardware-audio", "off"))
        assert m.encoding_preset() == "custom"

    def test_stock_video_with_accelerated_ml_is_not_stock(self, tmp_data_dir):
        """An install with the Stock switches but the native engine must report
        custom. Reporting "stock" there is the one lie this must never tell."""
        config = m.apply_encoding_preset("stock", {})
        config["ml_engine"] = "native"
        config["stock_ml"] = False
        assert m.encoding_preset(config) == "custom"

    def test_every_preset_names_the_ml_side(self, tmp_data_dir):
        """A position added to one table and not the other would apply half of
        itself and then report as custom immediately afterwards."""
        assert set(m.ENCODING_PRESETS) == set(m.PRESET_ML)
        assert set(m.ENCODING_PRESETS) == set(m.PRESET_SUMMARY)

    def test_the_ml_service_is_told_about_stock(self, tmp_path, tmp_data_dir):
        """config_env only forwards IMMICH_ACCEL*, so ML_STOCK has to be passed
        explicitly or the engine never hears about it and quietly runs Vision."""
        config = {"ml_dir": str(tmp_path)}
        venv_python = tmp_path / "venv" / "bin"
        venv_python.mkdir(parents=True, exist_ok=True)
        (venv_python / "python3").write_text("")
        (venv_python / "python3").chmod(0o755)

        spec = m._venv_ml_spec({**config, "stock_ml": True}, {})
        assert spec is not None and spec[2]["ML_STOCK"] == "true"
        spec = m._venv_ml_spec({**config, "stock_ml": False}, {})
        assert spec is not None and spec[2]["ML_STOCK"] == "false"

    def test_the_decode_switch_persists_too(self, tmp_data_dir):
        m.save_config({})
        m.cmd_encoding(self._args("hardware-decode", "off"))
        assert m.load_config()["env"]["IMMICH_ACCEL_HW_DECODE"] == "0"
        assert m.encoding_switch_on("hardware-decode") is False

    def test_the_two_switches_are_independent(self, tmp_data_dir):
        """Turning decode off must not disturb encoding, and the reverse. They
        gate different halves of the wrapper."""
        m.save_config({})
        m.cmd_encoding(self._args("hardware-decode", "off"))
        assert m.encoding_switch_on("hardware-video") is True
        m.cmd_encoding(self._args("hardware-video", "off"))
        assert m.encoding_switch_on("hardware-decode") is False
        m.cmd_encoding(self._args("hardware-decode", "on"))
        assert m.encoding_switch_on("hardware-video") is False

    # The one that actually protects the feature. cmd_encoding writes values
    # that ffmpeg-wrapper.sh reads, and the two decide "off" in different
    # languages: Python's bool_setting and the wrapper's _off(). If they ever
    # disagree, a switch reports off in the UI while the wrapper carries on
    # using the hardware, which is invisible until someone compares output.
    #
    # Every switch is checked, so adding one to ENCODING_SWITCHES without a
    # wrapper probe here fails rather than going quietly uncovered.
    PROBES = {
        # variable -> (args to run, marker that means "hardware is in use")
        "IMMICH_ACCEL_HW_VIDEO": (
            ["-i", "in.mov", "-c:v", "h264", "out.mp4"],
            "h264_videotoolbox",
        ),
        "IMMICH_ACCEL_HW_DECODE": (
            ["-i", "in.mov", "-frames:v", "1", "out.jpg"],
            "-hwaccel",
        ),
        "IMMICH_ACCEL_HW_AUDIO": (
            ["-i", "in.mov", "-c:a", "aac", "out.mp4"],
            "aac_at",
        ),
    }

    # Which switches are off unless asked for. The truthiness rule inverts with
    # the default in both implementations, so an unrecognised value keeps the
    # safer position rather than flipping behaviour.
    DEFAULT_OFF = {"IMMICH_ACCEL_HW_AUDIO"}

    def test_every_switch_means_the_same_thing_to_the_cli_and_the_wrapper(
        self, tmp_path
    ):
        wrapper = TestHardwareEncodingCanBeTurnedOff()
        for _, (variable, _description) in m.ENCODING_SWITCHES.items():
            assert variable in self.PROBES, (
                f"{variable} has no wrapper probe, so nothing checks that the "
                f"switch and the wrapper agree"
            )
            args, marker = self.PROBES[variable]
            # Mixed case and surrounding space included: the two
            # implementations used to disagree on exactly these, and a test
            # that only tries already-lowercase values never notices.
            for value in (
                "0", "false", "no", "1", "true", "yes", "", "off", "banana",
                "FALSE", "True", " 0", " YES ", "No", "1 ",
            ):
                default_on = variable not in self.DEFAULT_OFF
                python_says_on = m.bool_setting(
                    variable, default_on, {"env": {variable: value}}
                )
                out = wrapper._run(tmp_path, args, env={variable: value})
                wrapper_says_on = marker in out
                assert python_says_on == wrapper_says_on, (
                    f"{variable}={value!r}: the CLI says "
                    f"{'on' if python_says_on else 'off'} but the wrapper says "
                    f"{'on' if wrapper_says_on else 'off'}"
                )


class TestFfmpegWrapperQuickLookFallback:
    """ffmpeg's own HEVC decoder can hard-reject a stream (e.g. real HDR10/
    BT.2020 phone footage) that macOS's native AVFoundation decodes fine.
    The wrapper retries a failed single-frame thumbnail extraction via
    QuickLook before giving up. These drive the wrapper directly with stub
    ffmpeg/qlmanage/vips binaries — the real decoders are verified on-device.
    """

    WRAPPER = REPO_ROOT / "immich_accelerator" / "ffmpeg-wrapper.sh"

    # Args shaped like Immich's own video-thumbnail transcode call
    # (BaseConfig.getBaseOutputOptions / ThumbnailConfig.getFilterOptions):
    # `-frames:v 1` marks a single-frame extraction, `scale=...` carries the
    # target pixel size, and the output path is always the last argument.
    def _thumbnail_args(self, input_path, output_path, scale="-2:250"):
        return [
            "-skip_frame",
            "nointra",
            "-i",
            str(input_path),
            "-fps_mode",
            "vfr",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-vf",
            f"scale={scale}",
            "-f",
            "image2",
            str(output_path),
        ]

    def _bash_stub(self, path_, body):
        path_.write_text("#!/bin/bash\n" + body)
        path_.chmod(0o755)

    def _prepare_wrapper(self, tmp_path, real_ffmpeg):
        # Mirror __main__.py's own deploy-time substitution exactly, so the
        # tested form matches what actually ships rather than the checked-in
        # placeholder.
        content = self.WRAPPER.read_text().replace(
            'REAL_FFMPEG="/opt/homebrew/bin/ffmpeg"',
            f'REAL_FFMPEG="{real_ffmpeg}"',
        )
        assert 'REAL_FFMPEG="' + str(real_ffmpeg) in content, (
            "substitution didn't take — the placeholder string in the "
            "wrapper no longer matches what __main__.py replaces"
        )
        dst = tmp_path / "ffmpeg-wrapper-under-test.sh"
        dst.write_text(content)
        dst.chmod(0o755)
        return dst

    # A qlmanage stub that mimics real behavior closely enough for the
    # wrapper's own logic: it writes `<outdir>/<basename(input)>.png`,
    # discovered by parsing `-o <dir>` and taking the last arg as input,
    # exactly like the real `qlmanage -t -s N -o DIR INPUT` invocation.
    _QLMANAGE_SUCCEEDS = (
        'dir=""; prev=""\n'
        'for a in "$@"; do [[ "$prev" == "-o" ]] && dir="$a"; prev="$a"; done\n'
        'input="${@: -1}"\n'
        'echo FAKEPNG > "$dir/$(basename "$input").png"\n'
    )
    _QLMANAGE_FAILS = "exit 1\n"

    # The real call is `"$VIPS_BIN" copy "$QL_RESULT" "$OUTPUT"`.
    _VIPS_COPY = 'cp "$2" "$3"\n'

    def test_successful_ffmpeg_call_passes_through_unchanged(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg"
        self._bash_stub(ffmpeg, "exit 0\n")
        ql_marker = tmp_path / "qlmanage_ran"
        qlmanage = tmp_path / "qlmanage"
        self._bash_stub(qlmanage, f"touch {ql_marker}\nexit 1\n")
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        args = self._thumbnail_args(tmp_path / "in.mp4", tmp_path / "out.jpg")

        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert not ql_marker.exists(), "fallback must not run when ffmpeg succeeds"

    def test_full_transcode_failure_does_not_trigger_fallback(self, tmp_path):
        """A full transcode (no `-frames:v 1`) has no single "the frame" for
        QuickLook to hand back — the fallback must never fire for it, and
        ffmpeg's real exit code must propagate unchanged."""
        ffmpeg = tmp_path / "ffmpeg"
        self._bash_stub(ffmpeg, "exit 69\n")
        ql_marker = tmp_path / "qlmanage_ran"
        qlmanage = tmp_path / "qlmanage"
        self._bash_stub(qlmanage, f"touch {ql_marker}\n" + self._QLMANAGE_SUCCEEDS)
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        output = tmp_path / "out.mp4"

        result = subprocess.run(
            [
                "/bin/bash",
                str(wrapper),
                "-i",
                str(tmp_path / "in.mp4"),
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(output),
            ],
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (
            result.returncode == 69
        ), f"real ffmpeg exit code must propagate; got {result.returncode}"
        assert not ql_marker.exists(), "fallback must not run for a full transcode"
        assert not output.exists()

    # What ffmpeg actually prints when its HEVC decoder rejects a stream. The
    # fallback keys on this rather than on the exit code, so the stub has to
    # produce it: a bare non-zero exit is a different situation (a truncated
    # file, a bad seek) and must NOT be papered over.
    _DECODE_REJECTED_STDERR = (
        "echo '[hevc @ 0x7f8] Error while decoding stream #0:0: "
        "Invalid data found when processing input' >&2\n"
    )

    def test_single_frame_failure_recovers_via_quicklook_fallback(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg"
        self._bash_stub(ffmpeg, self._DECODE_REJECTED_STDERR + "exit 69\n")
        qlmanage = tmp_path / "qlmanage"
        self._bash_stub(qlmanage, self._QLMANAGE_SUCCEEDS)
        vips = tmp_path / "vips_stub.sh"
        self._bash_stub(vips, self._VIPS_COPY)
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        output = tmp_path / "out.jpg"
        args = self._thumbnail_args(tmp_path / "in.mp4", output)

        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            env={
                "PATH": f"{tmp_path}:/usr/bin:/bin",
                "IMMICH_ACCELERATOR_VIPS": str(vips),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert output.exists(), "fallback should have produced the output file"
        assert output.read_text().strip() == "FAKEPNG"
        assert "QuickLook/AVFoundation produced one instead" in result.stderr

    def test_a_non_decode_failure_is_not_papered_over(self, tmp_path):
        """A broken file must stay broken.

        The fallback used to fire on any non-zero exit, so a truncated upload
        or a seek past the end still got a poster frame out of QuickLook and
        exited 0: Immich recorded a corrupt asset as successfully thumbnailed
        and nobody ever found out. Only a decoder rejection qualifies now.
        """
        ffmpeg = tmp_path / "ffmpeg"
        self._bash_stub(
            ffmpeg, "echo 'in.mp4: No such file or directory' >&2\nexit 1\n"
        )
        qlmanage = tmp_path / "qlmanage"
        self._bash_stub(qlmanage, self._QLMANAGE_SUCCEEDS)
        vips = tmp_path / "vips_stub.sh"
        self._bash_stub(vips, self._VIPS_COPY)
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        output = tmp_path / "out.jpg"
        args = self._thumbnail_args(tmp_path / "in.mp4", output)

        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            env={
                "PATH": f"{tmp_path}:/usr/bin:/bin",
                "IMMICH_ACCELERATOR_VIPS": str(vips),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1, "ffmpeg's real failure must propagate"
        assert not output.exists(), "no thumbnail should be invented for a broken file"

    def test_single_frame_failure_falls_through_when_quicklook_also_fails(
        self, tmp_path
    ):
        """Never worse than without the fallback: if QuickLook can't recover
        anything either, propagate ffmpeg's real failure."""
        ffmpeg = tmp_path / "ffmpeg"
        self._bash_stub(ffmpeg, "exit 69\n")
        qlmanage = tmp_path / "qlmanage"
        self._bash_stub(qlmanage, self._QLMANAGE_FAILS)
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        output = tmp_path / "out.jpg"
        args = self._thumbnail_args(tmp_path / "in.mp4", output)

        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 69
        assert not output.exists()

    def test_fallback_only_fires_for_known_image_extensions(self, tmp_path):
        ffmpeg = tmp_path / "ffmpeg"
        self._bash_stub(ffmpeg, "exit 69\n")
        ql_marker = tmp_path / "qlmanage_ran"
        qlmanage = tmp_path / "qlmanage"
        self._bash_stub(qlmanage, f"touch {ql_marker}\n" + self._QLMANAGE_SUCCEEDS)
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        # .mp4 output with -frames:v 1 doesn't happen in real Immich usage,
        # but exercises the extension guard directly rather than relying on
        # Immich to never send it.
        args = self._thumbnail_args(tmp_path / "in.mp4", tmp_path / "out.mp4")

        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 69
        assert not ql_marker.exists()

    def test_hardware_encoder_remap_still_applies(self, tmp_path):
        """Regression check: restructuring `exec` into a captured run (so the
        wrapper can inspect ffmpeg's exit code) must not disturb the
        pre-existing VideoToolbox encoder remap."""
        ffmpeg = tmp_path / "ffmpeg"
        seen_args = tmp_path / "seen_args"
        self._bash_stub(ffmpeg, f'printf "%s\\n" "$@" > {seen_args}\nexit 0\n')
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)

        result = subprocess.run(
            [
                "/bin/bash",
                str(wrapper),
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                str(tmp_path / "out.mp4"),
            ],
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        received = seen_args.read_text().splitlines()
        assert "-hwaccel" in received and "videotoolbox" in received
        assert "h264_videotoolbox" in received
        assert "libx264" not in received
        assert (
            "-preset" not in received
        ), "software presets must be stripped for VideoToolbox"

    def test_thumbnail_job_gets_hardware_decode(self, tmp_path):
        """Immich's thumbnail and preview jobs send no -c:v at all, so there is
        no encoder to remap. Hardware decode still has to be injected."""
        ffmpeg = tmp_path / "ffmpeg"
        seen_args = tmp_path / "seen_args"
        self._bash_stub(ffmpeg, f'printf "%s\\n" "$@" > {seen_args}\nexit 0\n')
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        args = self._thumbnail_args(tmp_path / "in.mov", tmp_path / "out.jpg")

        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        received = seen_args.read_text().splitlines()
        assert received[:2] == [
            "-hwaccel",
            "videotoolbox",
        ], f"-hwaccel must be injected as an input option, before -i; got {received}"
        # Nothing else may change: no encoder was requested, so none is invented.
        assert not any(a.endswith("_videotoolbox") for a in received[2:])
        assert (
            received[2:] == args
        ), "the remaining arguments must pass through verbatim"

    def _capture_ffmpeg_args(self, tmp_path, args):
        """Run the wrapper against a stub ffmpeg that records its argv."""
        ffmpeg = tmp_path / "ffmpeg"
        seen_args = tmp_path / "seen_args"
        self._bash_stub(ffmpeg, f'printf "%s\\n" "$@" > {seen_args}\nexit 0\n')
        wrapper = self._prepare_wrapper(tmp_path, ffmpeg)
        result = subprocess.run(
            ["/bin/bash", str(wrapper), *args],
            env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return seen_args.read_text().splitlines()

    def _quality_value(self, received):
        assert received.count("-q:v") == 1, received
        return received[received.index("-q:v") + 1]

    # Immich's quality setting is a CRF number, 0-51, lower is better. The
    # VideoToolbox encoders ignore -crf outright and read -q:v on a 0-100
    # scale where higher is better, so the wrapper translates the number.
    # CRF 23 (Immich's default) maps to 59; the median matched quality
    # measured across six clips and five metrics at CRF 23 was 58.9.
    def test_crf_is_translated_for_videotoolbox(self, tmp_path):
        received = self._capture_ffmpeg_args(
            tmp_path,
            [
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "h264",
                "-preset",
                "ultrafast",
                "-crf",
                "23",
                str(tmp_path / "out.mp4"),
            ],
        )
        assert "-crf" not in received, "-crf means nothing to h264_videotoolbox"
        assert self._quality_value(received) == "59"

        # A leading zero must not be read as octal.
        received = self._capture_ffmpeg_args(
            tmp_path,
            [
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "h264",
                "-preset",
                "ultrafast",
                "-crf",
                "08",
                str(tmp_path / "out.mp4"),
            ],
        )
        assert self._quality_value(received) == "88"

    def test_incoming_quality_flag_is_rewritten_to_the_encoder_scale(self, tmp_path):
        """With CQ mode set to CQP, Immich sends `-q:v N` carrying the same
        CRF-scale number. Passed through untouched it inverts the setting:
        CRF 23 would execute as quality 23 out of 100."""
        received = self._capture_ffmpeg_args(
            tmp_path,
            [
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "h264",
                "-q:v",
                "23",
                str(tmp_path / "out.mp4"),
            ],
        )
        assert self._quality_value(received) == "59"

        # The stream-specific spelling carries the same number.
        received = self._capture_ffmpeg_args(
            tmp_path,
            [
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "h264",
                "-q:v:0",
                "23",
                str(tmp_path / "out.mp4"),
            ],
        )
        assert self._quality_value(received) == "59"

    def test_translated_quality_stays_inside_the_encoder_scale(self, tmp_path):
        """VideoToolbox rejects a quality outside 0-100. Values outside the
        0-51 range Immich's UI offers clamp to the ends of the scale."""
        best = self._capture_ffmpeg_args(
            tmp_path,
            [
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "hevc",
                "-crf",
                "0",
                str(tmp_path / "out.mp4"),
            ],
        )
        assert self._quality_value(best) == "100"
        worst = self._capture_ffmpeg_args(
            tmp_path,
            [
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "hevc",
                "-crf",
                "63",
                str(tmp_path / "out.mp4"),
            ],
        )
        assert self._quality_value(worst) == "1"

    def test_software_encode_keeps_its_own_crf(self, tmp_path):
        """The translation is only correct for VideoToolbox. An encoder the
        wrapper doesn't remap keeps the arguments Immich sent."""
        received = self._capture_ffmpeg_args(
            tmp_path,
            [
                "-i",
                str(tmp_path / "in.mov"),
                "-c:v",
                "libvpx-vp9",
                "-crf",
                "23",
                str(tmp_path / "out.webm"),
            ],
        )
        # No -hwaccel assertion. It was equivalent to "not remapped" until #153
        # separated decode from encode; decode is now requested for every input,
        # including software-encoded ones, which is the point of it.
        assert "-q:v" not in received
        assert received[received.index("-crf") + 1] == "23"


class TestPgKeepaliveShim:
    """The pg keepalive shim sets keepAlive on Immich's Postgres connections so
    a stateful firewall between worker and a remote DB can't reap idle
    connections (issue #74). Immich's source is never touched — the shim wraps
    the `pg` module via NODE_OPTIONS=--require."""

    SHIM_PATH = REPO_ROOT / "immich_accelerator" / "hooks" / "pg_keepalive_shim.js"

    def test_shim_file_exists(self):
        assert self.SHIM_PATH.exists(), f"hook shim missing: {self.SHIM_PATH}"

    def test_shim_is_referenced_by_cmd_start(self):
        src = (REPO_ROOT / "immich_accelerator" / "__main__.py").read_text()
        assert "pg_keepalive_shim.js" in src
        assert "NODE_OPTIONS" in src

    @pytest.mark.slow
    def test_shim_injects_keepalive_into_pg(self, tmp_path):
        """Run node with the shim preloaded against a fake `pg` module (no real
        DB), construct a Pool, and confirm keepAlive is injected while
        instanceof and an explicit caller value are both preserved."""
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")

        # Minimal stand-in for node-postgres: Pool/Client that stash their config.
        pgdir = tmp_path / "node_modules" / "pg"
        pgdir.mkdir(parents=True)
        (pgdir / "index.js").write_text(
            "class Pool { constructor(c){ this.options = c || {}; } }\n"
            "class Client { constructor(c){ this.options = c || {}; } }\n"
            "module.exports = { Pool, Client };\n"
        )

        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { Pool } = require('pg');\n"
            "const p = new Pool({ host: 'db' });\n"
            "console.log('KEEPALIVE:' + p.options.keepAlive);\n"
            "console.log('DELAY:' + p.options.keepAliveInitialDelayMillis);\n"
            "console.log('INSTANCE:' + (p instanceof Pool));\n"
            "const q = new Pool({ keepAlive: false });\n"
            "console.log('EXPLICIT:' + q.options.keepAlive);\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        out = result.stdout
        assert "KEEPALIVE:true" in out, out
        assert "DELAY:10000" in out, out
        assert "INSTANCE:true" in out, out  # Proxy preserves instanceof
        assert "EXPLICIT:false" in out, out  # never override an explicit choice

    @pytest.mark.slow
    def test_shim_disabled_via_env(self, tmp_path):
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        pgdir = tmp_path / "node_modules" / "pg"
        pgdir.mkdir(parents=True)
        (pgdir / "index.js").write_text(
            "class Pool { constructor(c){ this.options = c || {}; } }\n"
            "module.exports = { Pool };\n"
        )
        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { Pool } = require('pg');\n"
            "console.log('KEEPALIVE:' + new Pool({}).options.keepAlive);\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            cwd=str(tmp_path),
            env={"IMMICH_ACCEL_PG_KEEPALIVE": "0", "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "KEEPALIVE:undefined" in result.stdout, result.stdout


class TestJobRetryShim:
    """The job retry shim gives BullMQ jobs an unlimited attempt
    count with exponential backoff that resets whenever the accelerator
    restarts. Immich hardcodes `attempts: 1` (no retry) for every queue
    (config.repository.js), so a transient connection drop in a split
    deployment permanently fails the job. Immich's source is never touched —
    the shim interposes on `bullmq` via NODE_OPTIONS=--require.

    It can't wrap the `Queue`/`Worker` constructors or exports: empirically,
    against the real installed bullmq package,
    `Object.getOwnPropertyDescriptor(bullmq, 'Queue')` (same for `Worker`) is
    `{ configurable: false, get: fn, set: undefined }` — tslib's
    __exportStar re-export of a star-exported class. Both plain assignment
    and Object.defineProperty throw against that descriptor. The fake
    `bullmq` module below reproduces that exact non-configurable getter shape
    so this class of bug (caught only by testing against the real package,
    not the first version of this test) can't silently regress.

    The retry *count* is set producer-side, by wrapping
    `Queue.prototype.add`/`addBulk`. The retry *delay* has to be set
    worker-side: `Job.shouldRetryJob()` reads
    `this.queue.opts.settings.backoffStrategy`, and for a job being
    processed `this.queue` is the *Worker* instance handling it (verified
    against bullmq's source — `Worker extends QueueBase`, and
    `Job.fromJSON(this, ...)` is called with `this` bound to the Worker), not
    the producer Queue object `@nestjs/bullmq` originally constructed. So the
    shim also wraps `Worker.prototype.run` to inject the backoff strategy
    there before the worker starts processing.
    """

    SHIM_PATH = REPO_ROOT / "immich_accelerator" / "hooks" / "job_retry_shim.js"

    @staticmethod
    def _write_fake_bullmq(bullmqdir):
        # add()/addBulk() echo back the opts they received; run() returns the
        # backoffStrategy the shim installed, both so the test can assert on
        # exactly what the shim injected. Real bullmq does far more (tracing,
        # Redis calls, actual job processing); none of that matters here —
        # this shim only touches options objects on the way in.
        bullmqdir.mkdir(parents=True)
        (bullmqdir / "index.js").write_text(
            "class Queue {\n"
            "  constructor(name, opts){ this.name = name; this.opts = opts || {}; }\n"
            "  add(name, data, opts){ return { name, opts }; }\n"
            "  addBulk(jobs){ return jobs.map(j => ({ name: j.name, opts: j.opts })); }\n"
            "}\n"
            "class Worker {\n"
            "  constructor(name, processor, opts){ this.name = name; this.opts = opts || {}; }\n"
            "  run(){ return this.opts.settings && this.opts.settings.backoffStrategy; }\n"
            "}\n"
            # Getter-only, non-configurable — matches the real installed
            # bullmq package's compiled CJS index exactly (verified via
            # Object.getOwnPropertyDescriptor against node_modules/bullmq).
            "Object.defineProperty(exports, 'Queue', {\n"
            "  get() { return Queue; }, enumerable: true, configurable: false,\n"
            "});\n"
            "Object.defineProperty(exports, 'Worker', {\n"
            "  get() { return Worker; }, enumerable: true, configurable: false,\n"
            "});\n"
        )

    def test_shim_file_exists(self):
        assert self.SHIM_PATH.exists(), f"hook shim missing: {self.SHIM_PATH}"

    def test_shim_is_referenced_by_cmd_start(self):
        src = (REPO_ROOT / "immich_accelerator" / "__main__.py").read_text()
        assert "job_retry_shim.js" in src
        assert "NODE_OPTIONS" in src

    def test_shim_syntax_valid(self):
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        result = subprocess.run(
            [node, "--check", str(self.SHIM_PATH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"shim syntax error: {result.stderr}"

    @pytest.mark.slow
    def test_shim_raises_attempts_via_add_and_addbulk(self, tmp_path):
        """add()/addBulk() should get an unlimited attempts count
        and a backoff marker injected when the caller didn't ask for
        anything explicit, the way Immich's job.repository.js calls them —
        while an explicit caller-set attempts count survives untouched."""
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")

        bullmqdir = tmp_path / "node_modules" / "bullmq"
        self._write_fake_bullmq(bullmqdir)

        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { Queue } = require('bullmq');\n"
            "const q = new Queue('thumbnailGeneration', {\n"
            "  defaultJobOptions: { attempts: 1, removeOnComplete: true, removeOnFail: false }\n"
            "});\n"
            # No per-call opts (Immich's actual call shape) — should pick up defaults.
            "const r1 = q.add('metadata-extraction', {}, {});\n"
            "console.log('ATTEMPTS:' + r1.opts.attempts);\n"
            "console.log('BACKOFF_TYPE:' + r1.opts.backoff.type);\n"
            # An explicit non-default attempts count must survive untouched.
            "const r2 = q.add('x', {}, { attempts: 5 });\n"
            "console.log('EXPLICIT_ATTEMPTS:' + r2.opts.attempts);\n"
            # addBulk: first job gets defaults, second job's explicit count survives.
            "const bulk = q.addBulk([\n"
            "  { name: 'a', data: {}, opts: {} },\n"
            "  { name: 'b', data: {}, opts: { attempts: 7 } },\n"
            "]);\n"
            "console.log('BULK_A_ATTEMPTS:' + bulk[0].opts.attempts);\n"
            "console.log('BULK_B_ATTEMPTS:' + bulk[1].opts.attempts);\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        out = result.stdout
        # Unlimited on purpose: a job that stops retrying needs a human to
        # requeue it, which defeats running unattended. The hourly ceiling is
        # what makes that affordable, not a limit on attempts.
        assert f"ATTEMPTS:{2**53 - 1}" in out, out
        assert "BACKOFF_TYPE:immich-accel" in out, out
        assert (
            "EXPLICIT_ATTEMPTS:5" in out
        ), out  # never override a real explicit choice
        assert f"BULK_A_ATTEMPTS:{2**53 - 1}" in out, out
        assert "BULK_B_ATTEMPTS:7" in out, out

    @pytest.mark.slow
    def test_shim_disabled_via_env(self, tmp_path):
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        bullmqdir = tmp_path / "node_modules" / "bullmq"
        self._write_fake_bullmq(bullmqdir)
        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { Queue } = require('bullmq');\n"
            "const q = new Queue('x', { defaultJobOptions: { attempts: 1 } });\n"
            "const r = q.add('x', {}, {});\n"
            "console.log('ATTEMPTS:' + r.opts.attempts);\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            cwd=str(tmp_path),
            env={"IMMICH_ACCEL_JOB_RETRY": "0", "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "ATTEMPTS:undefined" in result.stdout, result.stdout

    @pytest.mark.slow
    def test_backoff_strategy_is_capped_exponential_per_job(self, tmp_path):
        """Worker.prototype.run must have the shim's backoff strategy
        installed in opts.settings. Calling it repeatedly for the same job
        should grow exponentially from the configured base delay, capped at
        the configured max — never bullmq's own uncapped 2^n growth — while
        a different job id starts its own count from the base delay."""
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        bullmqdir = tmp_path / "node_modules" / "bullmq"
        self._write_fake_bullmq(bullmqdir)
        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { Worker } = require('bullmq');\n"
            "const w = new Worker('thumbnailGeneration', null, {});\n"
            "const strategy = w.run();\n"
            "console.log('HAS_STRATEGY:' + (typeof strategy === 'function'));\n"
            "const jobA = { id: '1', queueName: 'thumbnailGeneration' };\n"
            "const jobB = { id: '2', queueName: 'thumbnailGeneration' };\n"
            "console.log('A1:' + strategy(undefined, undefined, undefined, jobA));\n"
            "console.log('A2:' + strategy(undefined, undefined, undefined, jobA));\n"
            "console.log('A3:' + strategy(undefined, undefined, undefined, jobA));\n"
            # A different job id must not inherit jobA's ramp.
            "console.log('B1:' + strategy(undefined, undefined, undefined, jobB));\n"
        )
        result = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            cwd=str(tmp_path),
            env={
                "IMMICH_ACCEL_JOB_RETRY_BACKOFF_MS": "1000",
                "IMMICH_ACCEL_JOB_RETRY_BACKOFF_MAX_MS": "3000",
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        out = result.stdout
        assert "HAS_STRATEGY:true" in out, out
        assert "A1:1000" in out, out  # base delay
        assert "A2:2000" in out, out  # 1000 * 2^1
        assert "A3:3000" in out, out  # 1000 * 2^2 = 4000, capped at 3000
        assert "B1:1000" in out, out  # independent counter, starts fresh

    @pytest.mark.slow
    def test_backoff_resets_across_process_restarts(self, tmp_path):
        """The whole point of tracking attempts in a process-local Map
        instead of bullmq's Redis-persisted attemptsMade: a fresh process
        (standing in for the accelerator restarting) must ramp from the
        short base delay again for a job id it has never seen before in
        *this* process, even though the job may have failed many times
        against the previous process."""
        import shutil

        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        bullmqdir = tmp_path / "node_modules" / "bullmq"
        self._write_fake_bullmq(bullmqdir)
        caller = tmp_path / "caller.js"
        caller.write_text(
            "const { Worker } = require('bullmq');\n"
            "const w = new Worker('q', null, {});\n"
            "const strategy = w.run();\n"
            "const job = { id: '42', queueName: 'q' };\n"
            # Simulate several prior failures within one process lifetime.
            "strategy(undefined, undefined, undefined, job);\n"
            "strategy(undefined, undefined, undefined, job);\n"
            "console.log('DELAY:' + strategy(undefined, undefined, undefined, job));\n"
        )
        env = {"IMMICH_ACCEL_JOB_RETRY_BACKOFF_MS": "1000", "PATH": "/usr/bin:/bin"}
        run1 = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        run2 = subprocess.run(
            [node, "--require", str(self.SHIM_PATH), str(caller)],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert run1.returncode == 0, run1.stderr
        assert run2.returncode == 0, run2.stderr
        # 3rd call for the same job id: 1000 * 2^2 = 4000, in both processes —
        # a real "remembers across restarts" bug would show run2 continuing
        # past where run1 left off instead of restarting at the same value.
        assert "DELAY:4000" in run1.stdout, run1.stdout
        assert "DELAY:4000" in run2.stdout, run2.stdout
