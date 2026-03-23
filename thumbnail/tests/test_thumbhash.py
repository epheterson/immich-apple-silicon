"""Tests for thumbhash_util module."""

import os

import pytest


def test_compute_thumbhash_returns_bytes():
    """ThumbHash of a real photo should be 15-25 bytes."""
    from thumbnail.thumbhash_util import compute_thumbhash

    input_path = "/nas/Pictures/iCloud/Eric/2020/09/18/62217014797__37D3FD0C-9F96-4C3E-903F-21AE1C2342AC.jpeg"
    if not os.path.exists(input_path):
        pytest.skip("Test image not available")
    result = compute_thumbhash(input_path)
    assert isinstance(result, bytes)
    assert 15 <= len(result) <= 25


def test_compute_thumbhash_deterministic():
    """Same image should always produce the same hash."""
    from thumbnail.thumbhash_util import compute_thumbhash

    input_path = "/nas/Pictures/iCloud/Eric/2020/09/18/62217014797__37D3FD0C-9F96-4C3E-903F-21AE1C2342AC.jpeg"
    if not os.path.exists(input_path):
        pytest.skip("Test image not available")
    h1 = compute_thumbhash(input_path)
    h2 = compute_thumbhash(input_path)
    assert h1 == h2


def test_compute_thumbhash_custom_size():
    """Custom size parameter should still produce valid hash."""
    from thumbnail.thumbhash_util import compute_thumbhash

    input_path = "/nas/Pictures/iCloud/Eric/2020/09/18/62217014797__37D3FD0C-9F96-4C3E-903F-21AE1C2342AC.jpeg"
    if not os.path.exists(input_path):
        pytest.skip("Test image not available")
    result = compute_thumbhash(input_path, size=50)
    assert isinstance(result, bytes)
    assert 15 <= len(result) <= 25
