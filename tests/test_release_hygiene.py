"""The CHANGELOG is release output, not just documentation.

`auto-tag` extracts the section for the version being tagged and publishes it
verbatim as the GitHub release notes, bounded by the *next* heading down. That
makes the set of headings load-bearing: delete one and the extraction runs past
it, so the release notes become every release since. That is not hypothetical.
Editing the 1.10.0 entry consumed the `## 1.9.0` heading, and the notes for
1.10.0 came out as 562 lines reaching back to 0.1.0. It was caught by hand
simulating the awk in ci.yml, which is not a control.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "CHANGELOG.md"
VERSION_FILE = REPO / "VERSION"

# Matches the headings ci.yml's awk anchors on: "## X.Y.Z" followed by
# whitespace. A heading it cannot match is invisible to the extraction.
HEADING = re.compile(r"^## (\d+\.\d+\.\d+)[ \t]", re.MULTILINE)


def headings() -> list[str]:
    return HEADING.findall(CHANGELOG.read_text())


def current_version() -> str:
    return VERSION_FILE.read_text().strip()


def extract_notes(version: str, previous: str) -> str:
    """Exactly what ci.yml's awk does, so this test fails when it would fail."""
    out, found = [], False
    for line in CHANGELOG.read_text().splitlines():
        if re.match(rf"^## {re.escape(previous)}[ \t]", line):
            break
        if re.match(rf"^## {re.escape(version)}[ \t]", line):
            found = True
            continue
        if found:
            out.append(line)
    return "\n".join(out)


class TestChangelogIsExtractable:
    def test_the_current_version_has_a_heading(self):
        assert current_version() in headings(), (
            f"VERSION says {current_version()} but CHANGELOG.md has no "
            f"'## {current_version()} ' heading, so the release notes would be empty"
        )

    def test_the_current_version_is_the_newest_heading(self):
        assert headings()[0] == current_version()

    def test_headings_are_unique(self):
        found = headings()
        assert len(found) == len(
            set(found)
        ), "a duplicate heading truncates the extraction early"

    def test_every_released_tag_still_has_a_heading(self):
        """The invariant that actually catches a deleted heading.

        Ordering checks do not: removing 1.9.0 leaves 1.10.0 then 1.8.0, still
        descending. Only the tags know a release happened.

        Floored at 1.3.0. Before that the rule was not yet enforced and four
        releases (1.1.1, 1.1.2, 1.1.3, 1.2.3) shipped with no entry, plus the
        0.1.x series which is documented as one range heading. Backfilling
        history is not this test's job; stopping the next deletion is.
        """
        floor = (1, 3, 0)
        try:
            tags = subprocess.run(
                ["git", "tag", "-l", "v*.*.*"],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pytest.skip("git unavailable")
        if not tags:
            # Not a skip in CI: this is the only check that catches a deleted
            # heading, and silently skipping is how it managed to never run.
            if os.environ.get("CI"):
                pytest.fail(
                    "no tags in this checkout, so this check cannot run. "
                    "The workflow needs fetch-depth: 0."
                )
            pytest.skip("no tags in this checkout (shallow clone)")
        documented = set(headings())
        missing = sorted(
            t[1:]
            for t in tags
            if tuple(int(p) for p in t[1:].split(".")) >= floor
            and t[1:] not in documented
        )
        assert not missing, f"released but no CHANGELOG heading: {missing}"

    def test_the_notes_stop_at_the_previous_release(self):
        found = headings()
        if len(found) < 2:  # pragma: no cover
            pytest.skip("only one release documented")
        notes = extract_notes(found[0], found[1])
        assert notes.strip(), "extracted release notes are empty"
        # Anchored to line starts: a plain substring test matches "### Added",
        # whose characters 1-3 are "## ", and fails on every healthy changelog.
        assert not re.search(r"^## ", notes, re.MULTILINE), (
            "the extracted notes contain another release heading, so the "
            "boundary heading is missing and the notes run on"
        )



def test_no_file_ships_git_conflict_markers():
    """An unresolved merge left conflict markers in docs/usage.md and
    v1.14.0 published it. The suite was green throughout, because nothing looks
    at prose. Cheap to check, and the failure mode is documentation that
    contradicts itself in front of a user.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent
    offenders = []
    for pattern in ("*.md", "*.py", "*.sh", "*.swift", "*.json", "*.yml"):
        for path in root.rglob(pattern):
            if any(
                part in {".git", "node_modules", ".build", "venv", "graphify-out"}
                for part in path.parts
            ):
                continue
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            # Built rather than written, so this file does not contain the
            # very markers it looks for and flag itself.
            for marker in ("<" * 7 + " ", "\n" + ">" * 7 + " "):
                if marker in text:
                    offenders.append(f"{path.relative_to(root)}: {marker.strip()}")
                    break

    assert not offenders, "unresolved conflict markers:\n  " + "\n  ".join(offenders)
