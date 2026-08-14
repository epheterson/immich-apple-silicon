"""Create a real Immich from scratch, the way the menu bar app does it.

The wizard's "Create Immich" step is the only path in the product that builds
infrastructure rather than configuring it, and until now nothing exercised it:
it was verified by reading. The failure mode is also the expensive kind, since
a stack pointed at the wrong folder is not undone by running setup again.

Stack creation is Docker work and platform-neutral, unlike everything after it
(extracting the native worker, Sharp, ffmpeg), so it can run on the Linux
runner that already stands up Immich for the detection test. What is asserted
here is exactly the contract the app depends on: given both paths and no
terminal, it builds the stack and never asks a question.

Marked `docker`, so it runs only in the fresh-install-docker job.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest


def _docker() -> str:
    found = shutil.which("docker")
    if not found:
        pytest.skip("docker not available")
    return found


def _compose_down(project_dir: Path, docker: str) -> None:
    subprocess.run(
        [docker, "compose", "down", "-v"],
        cwd=str(project_dir),
        capture_output=True,
        timeout=180,
    )


def _api_answers(timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                "http://localhost:2283/api/server/ping", timeout=5
            ) as r:
                if b"pong" in r.read():
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


@pytest.mark.docker
class TestCreateImmichFromScratch:
    def test_builds_the_stack_from_supplied_paths_without_asking(
        self, tmp_data_dir, tmp_path
    ):
        import immich_accelerator.__main__ as m

        docker = _docker()
        photos = tmp_path / "photos"
        photos.mkdir()
        (photos / "sample.txt").write_text("not really a photo")
        data = tmp_path / "immich-data"

        # No terminal, and any prompt is a failure rather than a hang: this is
        # the app's exact situation, and the whole point is that it completes
        # without one.
        with patch.object(m.sys.stdin, "isatty", return_value=False), patch.object(
            m, "ASSUME_YES", True
        ), patch("builtins.input", side_effect=AssertionError("must not prompt")):
            ok = m._fresh_install(docker, str(photos), str(data))

        assert ok is True, "fresh install reported failure"

        project = m.DATA_DIR
        compose = project / "docker-compose.yml"
        try:
            assert compose.is_file(), "no compose file was written"
            written = compose.read_text()
            assert str(photos) in written, "the photo path never reached the compose"
            env = (project / ".env").read_text()
            assert str(data) in env, "the data path never reached the environment"
            assert data.is_dir()

            assert _api_answers(), "the stack came up but Immich never answered"

            names = subprocess.run(
                [docker, "ps", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
            running = [n for n in names.split() if "immich" in n]
            assert len(running) >= 3, f"expected the full stack, saw {running}"

            # The thing the whole flow exists to enable: detection has to find
            # what we just built, or setup cannot configure against it.
            found = m.detect_immich(docker)
            assert found["version"], "detection could not read a version back"
        finally:
            _compose_down(project, docker)

    def test_a_bad_photo_path_fails_before_building_anything(
        self, tmp_data_dir, tmp_path
    ):
        """Cheap to get wrong from a GUI, and expensive to discover later."""
        import immich_accelerator.__main__ as m

        docker = _docker()
        with patch.object(m.sys.stdin, "isatty", return_value=False), patch.object(
            m, "ASSUME_YES", True
        ), patch("builtins.input", side_effect=AssertionError("must not prompt")):
            ok = m._fresh_install(
                docker, str(tmp_path / "does-not-exist"), str(tmp_path / "data")
            )

        assert ok is False
        assert not (
            m.DATA_DIR / "docker-compose.yml"
        ).exists(), "a stack was written for a photo path that does not exist"
