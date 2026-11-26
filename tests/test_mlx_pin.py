"""The weekly mlx check watches for versions newer than the one the ml fork pins."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_weekly_check_tracks_the_pinned_mlx():
    reqs = (ROOT / "ml" / "requirements.txt").read_text()
    pin = re.search(r"^mlx==([0-9.]+)\s*$", reqs, re.M)
    assert pin, "ml/requirements.txt must pin an exact, gate-tested mlx (mlx==X.Y.Z)"
    wf = (ROOT / ".github" / "workflows" / "upstream-sync-check.yml").read_text()
    watched = re.search(r'MLX_PINNED: "([0-9.]+)"', wf)
    assert watched and watched.group(1) == pin.group(1)
