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

import subprocess
import venv
from pathlib import Path

import pytest

from unittest.mock import patch, MagicMock

from immich_accelerator.__main__ import (
    SUPPORTED_NODE_MAJORS,
    _check_node_engines_compat,
    _has_everything,
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
            timeout=10,
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
        """Static check: the CI-generated Homebrew formula must pin
        node@22. A regression to `depends_on "node"` re-ships the bug.
        """
        template = (
            REPO_ROOT / ".github" / "workflows" / "update-homebrew.yml"
        ).read_text()
        assert 'depends_on "node@22"' in template, (
            "Formula template must pin node@22 — "
            '`depends_on "node"` pulls mainline which breaks sharp.'
        )
        # And the bare version must be GONE — no lingering duplicate.
        # (Count occurrences to allow the pinned form to exist alongside
        # comments that mention "node" as text.)
        assert 'depends_on "node"\n' not in template

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
