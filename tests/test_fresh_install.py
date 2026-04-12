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

from immich_accelerator.__main__ import _has_everything, _needs_core_plugin

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
