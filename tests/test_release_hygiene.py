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


def test_no_test_is_shadowed_by_a_later_definition():
    """Two methods with one name in a class: Python keeps the last, silently.

    The first is then dead code that still reads like coverage, which is worse
    than having no test at all. This happened for real: resolving a merge
    between two branches that both appended to the same test class duplicated
    seven methods, and the suite stayed green because the surviving copies
    passed. Nothing pointed at the seven that had stopped running.
    """
    import ast
    import collections
    from pathlib import Path

    offenders = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            for name, count in collections.Counter(names).items():
                if count > 1:
                    offenders.append(f"{path.name}::{node.name}::{name} ({count}x)")
        # Module-level functions shadow each other the same way.
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        for name, count in collections.Counter(names).items():
            if count > 1:
                offenders.append(f"{path.name}::{name} ({count}x)")

    assert not offenders, "shadowed definitions never run:\n  " + "\n  ".join(offenders)



def test_the_swift_preset_mirror_matches_the_python_one():
    """The app keeps its own copy of the preset table so the settings window
    can show the current position without shelling out on every render.

    A copy drifts. It already has, twice: once shipping a pane that would
    report Stock, asserting output identical to Docker, on an install running
    Vision and mlx, and once showing Apple Silicon selected on a config the CLI
    called custom. Nothing but this test connects the two files, so parse the
    Swift and compare rather than trusting a comment to be read.
    """
    import re
    from pathlib import Path

    import immich_accelerator.__main__ as m

    swift = (
        Path(__file__).parent.parent
        / "menubar/Sources/AcceleratorBar/StatusModel.swift"
    ).read_text()
    table = swift[swift.index("encodingPresets:") : swift.index("encodingDefaultOff")]

    parsed = {}
    for entry in re.finditer(r'\("([a-z-]+)",\s*\[(.*?)\]\)', table, re.S):
        name, switches = entry.groups()
        parsed[name] = {
            k: v == "true"
            for k, v in re.findall(r'"(\w+)":\s*(true|false)', switches)
        }

    assert parsed, "could not parse the Swift preset table; update this test"
    assert set(parsed) == set(m.ENCODING_PRESETS), (
        f"Swift has {sorted(parsed)}, Python has {sorted(m.ENCODING_PRESETS)}"
    )
    for name, switches in parsed.items():
        assert switches == m.ENCODING_PRESETS[name], name
