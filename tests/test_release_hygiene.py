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



def test_the_swift_preset_names_match_the_python_ones():
    """Two Swift files name the positions, and both must agree with Python.

    Checking only StatusModel is how a rename landed in two of three places
    and shipped a control that matched nothing and sent the CLI a name it
    rejected, while this suite stayed green.
    """
    import re
    from pathlib import Path

    import immich_accelerator.__main__ as m

    root = Path(__file__).parent.parent
    expected = set(m.ENCODING_PRESETS)

    model = (root / "menubar/Sources/AcceleratorBar/StatusModel.swift").read_text()
    table = model[model.index("encodingPresets:"):model.index("encodingDefaultOff")]
    mirror = {}
    for entry in re.finditer(r'\("([a-z-]+)",\s*\[(.*?)\]\)', table, re.S):
        name, switches = entry.groups()
        mirror[name] = {
            k: v == "true"
            for k, v in re.findall(r'"(\w+)":\s*(true|false)', switches)
        }
    assert mirror, "could not parse StatusModel's table; update this test"
    assert set(mirror) == expected, f"StatusModel has {sorted(mirror)}"
    for name, switches in mirror.items():
        assert switches == m.ENCODING_PRESETS[name], name

    # The slice above stops at `encodingDefaultOff`, so the two things that
    # decide how a switch reads are outside it: which switches default off,
    # and the words the wrapper accepts as on and as off. Both were pinned by
    # nothing, and emptying the set left the whole suite green while the pane
    # would have shown hardware audio on by default and the CLI off.
    default_off = re.search(r"encodingDefaultOff: Set<String> = \[(.*?)\]", model)
    assert default_off, "could not parse StatusModel's default-off set; update this test"
    mirrored_off = set(re.findall(r'"(\w+)"', default_off.group(1)))
    assert mirrored_off == set(m._DEFAULT_OFF), f"StatusModel has {sorted(mirrored_off)}"

    # The negated branch is written first, so the first list is the off words.
    # Which list is which is the meaning; the order inside one is not.
    words = [
        set(re.findall(r'"([a-z0-9]+)"', group))
        for group in re.findall(r'\[((?:\s*"[a-z0-9]+",?)+)\]\.contains\(value\)', model)
    ]
    assert len(words) == 2, "could not parse StatusModel's truth words; update this test"
    assert words == [set(m._ENV_OFF), set(m._ENV_ON)], f"StatusModel has {words}"

    pane = (root / "menubar/Sources/AcceleratorBar/SettingsView.swift").read_text()
    listing = pane[pane.index("static let positions:"):pane.index("private var currentPosition")]
    names = set(re.findall(r'\("([a-z-]+)",\s*"', listing)) - {"custom"}
    assert names, "could not parse SettingsView's list; update this test"
    assert names == expected, f"SettingsView has {sorted(names)}, Python {sorted(expected)}"

    # The same sentences are shown by the CLI and by the pane, so they are
    # written twice and drifted the first time one was edited: the CLI still
    # said "the most CPU of the three" after there stopped being three.
    described = dict(
        re.findall(r'\("([a-z]+)",\s*"[A-Za-z]+",\s*\n?\s*"((?:[^"\\]|\\.)*)"\)', listing)
    )
    assert set(described) == expected | {"custom"}, (
        f"could not parse SettingsView's descriptions; got {sorted(described)}"
    )
    for name, text in described.items():
        assert text.replace("\\'", "'") == m.PRESET_DETAIL[name], (
            f"the pane and the CLI describe {name} differently"
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
    # .js and .html included: the hook shims and the dashboard are tracked
    # source that a bad merge can land in just as easily as the rest.
    for pattern in (
        "*.md", "*.py", "*.sh", "*.swift", "*.json", "*.yml", "*.js", "*.html",
    ):
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


def test_settings_never_restart_the_service_unconditionally():
    """`brew services restart` starts a stopped service.

    So a settings change that calls it directly does not apply a setting, it
    starts an accelerator somebody deliberately stopped and sets it
    processing. That shipped once and was reintroduced hours after being
    fixed, both times by someone reaching for the obvious verb. The only
    caller allowed to restart without checking first is the menu's explicit
    Restart command, which is a person asking for exactly that.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent / "menubar/Sources/AcceleratorBar"
    allowed = {
        # The menu's Restart item: the user asked to restart.
        "MenuView.swift",
        # Declares it, and calls it only inside applyToRunningService, which
        # has already established that brew has the service started.
        "Actions.swift",
    }
    for swift in sorted(root.glob("*.swift")):
        if swift.name in allowed:
            continue
        assert "restartService" not in swift.read_text(), (
            f"{swift.name} restarts the service directly. Apply changes "
            f"through Actions.applyToRunningService, which restarts only "
            f"when brew already has it running."
        )

    actions = (root / "Actions.swift").read_text()
    body = actions[actions.index("static func applyToRunningService"):]
    body = body[:body.index("\n    /// ", 1)]
    assert "brewHasItStarted" in body and "await restartService()" in body, (
        "applyToRunningService must gate its restart on brewHasItStarted"
    )


def test_no_photograph_is_embedded_in_the_tree():
    """render-formula.sh installs this tree wholesale, so anything committed
    here is redistributed to every Homebrew user.

    A base64 photograph of identifiable people shipped in v1.15.0 that way,
    with no source and no licence recorded, and was only caught by a
    contributor reading the diff (#167). Test images are fetched on demand and
    cached outside the repo instead. The two embedded JPEGs that remain are
    synthetic: a generated text strip and a gradient, neither depicting anyone.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent
    # A JPEG this large is a photograph. The synthetic ones are ~2KB of base64;
    # the photograph that prompted this was 27KB of bytes, 36KB encoded.
    limit = 8000
    for path in sorted((root / "scripts").glob("*.py")):
        text = path.read_text()
        for name, literal in re.findall(
            r"^(_\w*JPEG\w*|_\w*IMAGE\w*)\s*=\s*(.+?)(?=^\w|\Z)",
            text,
            re.M | re.S,
        ):
            encoded = "".join(re.findall(r'"([A-Za-z0-9+/=]{40,})"', literal))
            assert len(encoded) < limit, (
                f"{path.name}: {name} embeds {len(encoded)} base64 characters. "
                f"An image that size is a photograph, and committing it here "
                f"redistributes it to every Homebrew user. Fetch it on demand "
                f"and cache it outside the repo, the way FACE_IMAGE and "
                f"native-ml-full-benchmark.py's IMAGE_SOURCES do, and record "
                f"where it came from."
            )


def test_the_byte_for_byte_claim_names_its_exception():
    """The Software position is described as byte-identical to Docker in four
    places, and the QuickLook thumbnail fallback is not gated on any of the
    hardware switches, so it is live in Software exactly as in Hardware.

    When it fires the thumbnail did not come from ffmpeg at all. A claim that
    does not say so overclaims, which is worse than a plainer one. Raised by
    @RxChi1d in #166.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent
    for rel in (
        "immich_accelerator/__main__.py",
        "menubar/Sources/AcceleratorBar/SettingsView.swift",
        "docs/usage.md",
        "CHANGELOG.md",
    ):
        text = (root / rel).read_text()
        # Normalised, because three of the four wrap the sentence across lines.
        flat = " ".join(text.split())
        if "byte for byte what Docker produces" not in flat:
            continue
        assert "QuickLook" in flat, (
            f"{rel} claims byte-for-byte output but never mentions the "
            f"QuickLook fallback, which produces thumbnails ffmpeg did not."
        )
