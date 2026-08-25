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
import inspect
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

    A base64 photograph shipped in v1.15.0 that way, with no source and no
    licence recorded, and was only caught by a contributor reading the
    diff (#167). Test images are fetched on demand and
    cached outside the repo instead. The two embedded JPEGs that remain are
    synthetic: a generated text strip and a gradient, neither depicting anyone.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent
    # A JPEG this large is a photograph. The synthetic ones are ~2KB of base64;
    # the photograph that prompted this was 27KB of bytes, 36KB encoded.
    limit = 8000
    # Every directory the formula installs, not just scripts/. The docstring
    # above is the argument for the wider net: render-formula.sh takes the
    # tree wholesale, so a photograph under immich_accelerator/ or tests/
    # ships exactly as readily and would have passed this untouched.
    shipped = [
        p
        for sub in ("scripts", "immich_accelerator", "tests", "menubar/Sources")
        for p in sorted((root / sub).rglob("*"))
        if p.suffix in {".py", ".sh", ".swift"} and p.is_file()
    ]
    assert len(shipped) > 10, f"found only {len(shipped)} files to scan"
    for path in shipped:
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


def test_the_untrusted_tap_words_match_between_swift_and_python():
    """The CLI and the app each decide independently whether Homebrew is
    refusing to load our formula, by matching text in brew's output.

    If one recognises a refusal the other does not, the app tells the user
    updates are fine while `status` says they are blocked, or the reverse.
    The words are brew's, not ours, so they are a fact about the environment
    and both copies have to agree on it.
    """
    from pathlib import Path

    import immich_accelerator.__main__ as m

    root = Path(__file__).parent.parent
    actions = (root / "menubar/Sources/AcceleratorBar/Actions.swift").read_text()
    body = actions[actions.index("static func brewRefusesTap"):]
    body = body[:body.index("\n    }")]
    # Both sides also match the formula name to anchor the refusal to our own
    # tap; that is not one of the phrases being compared here.
    swift_words = {
        w for w in re.findall(r'contains\("([^"]+)"\)', body)
        if " " in w
    }
    assert swift_words, "could not parse the Swift matcher; update this test"

    source = inspect.getsource(m.brew_refuses_our_tap)
    python_words = set(re.findall(r'"([a-z ]+)" in line', source))
    assert python_words, "could not parse the Python matcher; update this test"

    assert swift_words == python_words, (
        f"Swift matches {sorted(swift_words)}, Python matches "
        f"{sorted(python_words)}"
    )


# What a "#N" in the tree has to survive to count as an issue citation. Both
# exclusions are structural rather than a list of known values: the first
# version of this listed one grey by value and promptly missed the four-digit
# one with an alpha channel on the very next line of the same stylesheet.
_CSS_PROPERTY = re.compile(r"\b(color|background|border|shadow|fill|stroke)\b")


def _is_citation(ref: str, line: str) -> bool:
    # A CSS hex colour in the dashboard's inline stylesheet. An all-digit grey
    # is indistinguishable from an issue number on its own, so the declaration
    # around it is what settles it.
    if _CSS_PROPERTY.search(line) and re.fullmatch(r"#[0-9a-fA-F]{3,8}", ref):
        return False
    # ffmpeg's stream syntax, "#0:0", inside test fixtures. There is no issue
    # zero, so this needs no context to rule out.
    return ref != "#0"


def _issue_citations():
    from pathlib import Path

    root = Path(__file__).parent.parent
    found = {}
    for sub in ("immich_accelerator", "menubar/Sources", "tests", "scripts"):
        for path in sorted((root / sub).rglob("*")):
            if path.suffix not in {".py", ".sh", ".swift"} or not path.is_file():
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                for ref in re.findall(r"#[0-9a-fA-F]{1,8}\b", line):
                    if not _is_citation(ref, line):
                        continue
                    if not ref.lstrip("#").isdigit():
                        continue
                    found.setdefault(ref, []).append(
                        f"{path.relative_to(root)}:{n}"
                    )
    return found


@pytest.mark.slow
def test_every_issue_reference_points_at_a_real_issue():
    """A comment citing an issue number is a claim about where a defect was
    reported, and a wrong number sends the next reader somewhere unrelated.

    This shipped: the QuickLook dimension defect was cited as "#21", which is
    an internal task number here and a merged PR about fresh-install dashboard
    regressions on GitHub. The number resolved, so nothing looked broken.

    Existence is all this can check. It cannot tell that a real number is the
    wrong one, so the rule that matters is still not to write a number down
    without opening it.
    """
    import json
    import shutil
    import subprocess

    if not shutil.which("gh"):
        pytest.skip("gh is not installed")

    missing = []
    for ref, sites in sorted(_issue_citations().items()):
        number = ref.lstrip("#")
        proc = subprocess.run(
            ["gh", "issue", "view", number, "--json", "number"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["gh", "pr", "view", number, "--json", "number"],
                capture_output=True, text=True,
            )
        if proc.returncode != 0:
            if "rate limit" in (proc.stderr or "").lower():
                pytest.skip("GitHub rate limit")
            missing.append(f"{ref} cited at {', '.join(sites)}")

    assert not missing, "these reference nothing that exists:\n  " + "\n  ".join(missing)


def _parity():
    """Load scripts/ml-parity.py as a module; it is a script, not a package."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "ml-parity.py"
    spec = importlib.util.spec_from_file_location("ml_parity", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parity_cosine_is_actually_cosine():
    """Every parity number reported so far rests on this function.

    A similarity that is subtly wrong would have been reported as fact, and
    "our embeddings agree with Docker's at 0.97" is exactly the kind of claim
    nobody re-derives.
    """
    m = _parity()
    assert m.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert m.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert m.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    # Scale must not matter: embeddings from two runtimes differ in magnitude.
    assert m.cosine([3.0, 4.0], [30.0, 40.0]) == pytest.approx(1.0)
    with pytest.raises(RuntimeError):
        m.cosine([1.0, 0.0], [1.0, 0.0, 0.0])
    with pytest.raises(RuntimeError):
        m.cosine([0.0, 0.0], [1.0, 0.0])


def test_parity_iou_is_actually_iou():
    m = _parity()
    box = {"x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 10.0}
    assert m.iou(box, box) == pytest.approx(1.0)
    # Half overlap: intersection 50, union 150.
    half = {"x1": 5.0, "y1": 0.0, "x2": 15.0, "y2": 10.0}
    assert m.iou(box, half) == pytest.approx(50 / 150)
    apart = {"x1": 20.0, "y1": 20.0, "x2": 30.0, "y2": 30.0}
    assert m.iou(box, apart) == 0.0
    # Touching edges are not overlapping.
    touching = {"x1": 10.0, "y1": 0.0, "x2": 20.0, "y2": 10.0}
    assert m.iou(box, touching) == 0.0


def test_parity_does_not_call_two_faces_a_match_just_because_the_counts_agree():
    """The trap this function exists to avoid: one face each, in different
    places, is not agreement, and a count comparison would call it that."""
    m = _parity()

    def face(x):
        return {"boundingBox": {"x1": x, "y1": 0.0, "x2": x + 10, "y2": 10.0}}

    same = m.compare_faces([face(0)], [face(0)])
    assert same["matched"] == 1 and same["mean_iou"] == pytest.approx(1.0)

    elsewhere = m.compare_faces([face(0)], [face(100)])
    assert elsewhere["ref_count"] == elsewhere["count"] == 1
    assert elsewhere["matched"] == 0, "boxes that do not overlap are not a match"
    assert elsewhere["mean_iou"] == 0.0

    # One reference face must not be matched twice by two of ours.
    two = m.compare_faces([face(0)], [face(0), face(1)])
    assert two["matched"] == 1


def test_parity_ocr_compares_the_words():
    m = _parity()
    same = m.compare_ocr({"text": ["Exit", "42B"]}, {"text": ["exit ", "42b"]})
    assert same["shared"] == 2, "case and padding are not a real difference"
    assert not same["only_ref"] and not same["only_other"]

    missed = m.compare_ocr({"text": ["Exit", "42B"]}, {"text": ["Exit"]})
    assert missed["shared"] == 1 and missed["only_ref"] == ["42b"]


def test_face_detection_does_not_go_back_to_detecting_with_the_landmarks_pass():
    """native-ml has no test target, so this is the only thing standing
    between a refactor and silently losing half the faces again.

    Structural, because the real check needs Vision, real images and an
    Apple Silicon machine: that is scripts/native-ml-preflight.py and
    scripts/ml-parity.py, neither of which runs in CI. What this pins is the
    shape the measurement depended on. Using VNDetectFaceLandmarksRequest to
    do the detecting found 20 of 48 reference faces and reported confidence
    1.000 for every one of them; detecting with VNDetectFaceRectanglesRequest
    and landmarking those observations found 25 of 48 with real scores.
    """
    from pathlib import Path

    src = (Path(__file__).parent.parent
           / "native-ml/Sources/immich-ml-native/FaceDetect.swift").read_text()
    # Comments stripped first. The comment above this code explains what the
    # landmarks request used to do, so a naive search finds that class name
    # before the rectangles one and the ordering check fails on prose.
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("//")
    )
    body = code[code.index("func detectFacesWithLandmarks"):]

    assert "VNDetectFaceRectanglesRequest" in body, (
        "detection must run as a rectangles request; the landmarks request "
        "finds fewer faces and reports confidence 1.000 for all of them"
    )
    assert "inputFaceObservations" in body, (
        "the landmarks pass must run on the detected observations, or the "
        "aligner loses the five points it needs"
    )
    # Order matters: rectangles first, then landmarks seeded from it.
    assert (body.index("VNDetectFaceRectanglesRequest")
            < body.index("VNDetectFaceLandmarksRequest")), (
        "the rectangles request must run first and feed the landmarks pass"
    )


def _preflight():
    """Load scripts/ml-preflight.py as a module. Importing it must not reach
    the network: the fetch belongs to face_jpeg, not to import."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "ml-preflight.py"
    spec = importlib.util.spec_from_file_location("ml_preflight", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_offline_face_image_override_is_used_verbatim(tmp_path):
    """--face-image is the escape hatch for running the gate without network.

    Untested, it would be discovered broken by the person who needed it most:
    someone on a machine that cannot reach the internet, mid-release.
    """
    m = _preflight()
    supplied = tmp_path / "mine.jpg"
    supplied.write_bytes(b"\xff\xd8\xff not-really-a-jpeg")

    # A cache directory that does not exist, so anything but the override
    # would have to hit the network and would fail the test loudly.
    m.FACE_CACHE = tmp_path / "cache-that-is-not-there"
    m.FACE_IMAGE = ("http://127.0.0.1:9/unreachable.jpg", "test")

    assert m.face_jpeg(str(supplied)) == supplied.read_bytes()
    assert not m.FACE_CACHE.exists(), (
        "an override must not populate the cache; the next run would then "
        "silently use someone's one-off image as the gate's face"
    )


def test_a_cached_face_image_is_reused_without_fetching(tmp_path):
    m = _preflight()
    m.FACE_IMAGE = ("http://127.0.0.1:9/unreachable.jpg", "test")
    m.FACE_CACHE = tmp_path / "cache"
    m.FACE_CACHE.mkdir()
    (m.FACE_CACHE / "unreachable.jpg").write_bytes(b"cached-bytes")

    # The URL is unroutable, so this can only pass by reading the cache.
    assert m.face_jpeg() == b"cached-bytes"


def test_a_failed_fetch_is_fatal_and_leaves_no_partial_file(tmp_path):
    """A truncated cache file would be read as valid by every later run, and
    the gate would be testing the detector against a broken image."""
    m = _preflight()
    m.FACE_IMAGE = ("http://127.0.0.1:9/unreachable.jpg", "test")
    m.FACE_CACHE = tmp_path / "cache"

    with pytest.raises(RuntimeError) as caught:
        m.face_jpeg()
    assert "--face-image" in str(caught.value), (
        "the error must name the way out, since this fires on a machine "
        "with no network"
    )
    leftovers = list(m.FACE_CACHE.glob("*")) if m.FACE_CACHE.exists() else []
    assert not leftovers, f"a failed fetch left {leftovers} behind"


def test_a_non_image_response_is_never_cached(tmp_path, monkeypatch):
    """A captive portal answering the image URL with an HTML login page is a
    200, and caching it poisons every later run: the faces check then fails
    with "found no faces in an image that contains one", which reads as a
    detector regression on the one gate that must not cry wolf.
    """
    import urllib.request

    m = _preflight()
    m.FACE_IMAGE = ("http://example.invalid/face.jpg", "test")
    m.FACE_CACHE = tmp_path / "cache"

    class FakePortal:
        def read(self):
            return b"<!doctype html><title>Sign in to the wifi</title>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakePortal())

    with pytest.raises(RuntimeError) as caught:
        m.face_jpeg()
    assert "did not return a JPEG" in str(caught.value)
    assert not list(m.FACE_CACHE.glob("*")), (
        "the non-image response was cached, so every later run reads it back"
    )


def test_parity_matching_prefers_the_best_overlap_not_list_order():
    """Walking the reference list in order let a poor early match consume the
    box a later reference face overlapped almost exactly, understating both
    the match count and the mean overlap the docs quote."""
    m = _parity()

    def face(x, w=10):
        return {"boundingBox": {"x1": x, "y1": 0.0, "x2": x + w, "y2": 10.0}}

    # A overlaps X slightly; B overlaps X almost exactly. A comes first.
    result = m.compare_faces([face(0, 40), face(30)], [face(30)])
    assert result["matched"] == 1
    assert result["mean_iou"] > 0.9, (
        f"matched the wrong pair: mean IoU {result['mean_iou']:.3f} means the "
        f"first reference face consumed the box the second one fits"
    )


def test_parity_ocr_recall_does_not_punish_repeated_words():
    """A sign reading EXIT twice, read correctly twice, is a perfect read.
    Counting duplicates in the denominator but not the numerator scored it
    0.5 while also calling the two sets identical."""
    m = _parity()
    both = {"text": ["EXIT", "EXIT"]}
    r = m.compare_ocr(both, both)
    assert r["shared"] == r["ref_count"] == 2
    assert not r["only_ref"] and not r["only_other"]


def test_parity_headline_iou_weights_by_faces_not_by_image():
    """The published average overlap is over matched faces. A mean of
    per-image means gives a one-face photo the same weight as a group shot."""
    m = _parity()
    rows = [
        {"engine": "ours", "task": "faces", "ref_count": 1, "count": 1,
         "matched": 1, "mean_iou": 1.0, "min_iou": 1.0},
        {"engine": "ours", "task": "faces", "ref_count": 9, "count": 9,
         "matched": 9, "mean_iou": 0.5, "min_iou": 0.5},
    ]
    summary = m.summarise(rows, {"ours": {"faces": []}}, "stock")
    faces = next(s for s in summary if s["task"] == "faces")
    assert faces["mean_iou"] == pytest.approx((1.0 * 1 + 0.5 * 9) / 10), (
        f"got {faces['mean_iou']:.4f}; an unweighted mean would be 0.75"
    )


def test_the_two_brew_calls_that_check_for_updates_may_still_fetch():
    """A tap is a local git clone. `brew outdated` refreshing it is the only
    thing that does, so suppressing the auto-update on the update check makes
    it permanently answer "nothing to do": Sparkle updates the app and the CLI
    core never follows it. Setting one constant on all eight brew calls did
    exactly that, which is why the two groups are now named apart.
    """
    root = Path(__file__).resolve().parent.parent
    src = (root / "menubar/Sources/AcceleratorBar/Actions.swift").read_text()

    def env_block(name: str) -> str:
        start = src.index(f"static let {name} = [")
        return src[start:src.index("]", start)]

    assert "HOMEBREW_NO_AUTO_UPDATE" in env_block("brewEnv")
    assert "HOMEBREW_NO_AUTO_UPDATE" not in env_block("brewEnvAllowingFetch"), (
        "the update check is back to never seeing a new version in the tap"
    )

    # The two that ask what version exists must use the fetching environment.
    for func in ("coreOutdated", "upgradeCore"):
        start = src.index(f"func {func}(")
        body = src[start:src.index("\n    }", start)]
        assert "brewEnvAllowingFetch" in body, (
            f"{func} cannot see a new formula, so upgrades stop happening"
        )

    # And no brew call may go out with neither, which is how two of the eight
    # ended up without the environment when it was written by hand.
    calls = re.findall(r"run\(\s*brew,.*?\n(?:.*?\n)??.*?\)", src, re.S)
    assert len(calls) >= 8, f"only found {len(calls)} brew calls; update this test"
    for call in calls:
        assert "brewEnv" in call, f"brew call with no environment at all:\n{call}"


def test_the_app_and_the_cli_agree_on_reading_a_pidfile_start_time():
    """Both read the same pidfiles, so a rule that holds on one side and not
    the other means Settings and `status` disagree about whether a service is
    running. Pinning ps to LC_ALL=C fixes what is read, not what is already
    stored, so a byte-for-byte compare against a pidfile written before the
    pin is a guaranteed mismatch rather than an intermittent one.
    """
    root = Path(__file__).resolve().parent.parent
    src = (root / "menubar/Sources/AcceleratorBar/StatusModel.swift").read_text()

    assert "func sameStart(" in src, "the Swift side has no comparison rule"
    start = src.index("static func pidAlive(")
    body = src[start:src.index("\n    }", start)]
    assert "sameStart(actualStart, storedStart)" in body
    assert "actualStart != storedStart" not in body, (
        "back to comparing byte for byte, which deletes healthy pidfiles "
        "written before 1.16 by any locale that is not C"
    )
