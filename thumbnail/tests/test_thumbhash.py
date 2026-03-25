"""Tests for thumbhash_util module."""

import os

import pytest

# Set TEST_IMAGE_PATH to a JPEG file to run these tests.
TEST_IMAGE = os.environ.get("TEST_IMAGE_PATH", "")


def test_compute_thumbhash_returns_bytes():
    """ThumbHash of a real photo should be 15-25 bytes."""
    from thumbnail.thumbhash_util import compute_thumbhash

    if not TEST_IMAGE or not os.path.exists(TEST_IMAGE):
        pytest.skip("Set TEST_IMAGE_PATH to a JPEG file")
    result = compute_thumbhash(TEST_IMAGE)
    assert isinstance(result, bytes)
    assert 15 <= len(result) <= 25


def test_compute_thumbhash_deterministic():
    """Same image should always produce the same hash."""
    from thumbnail.thumbhash_util import compute_thumbhash

    if not TEST_IMAGE or not os.path.exists(TEST_IMAGE):
        pytest.skip("Set TEST_IMAGE_PATH to a JPEG file")
    h1 = compute_thumbhash(TEST_IMAGE)
    h2 = compute_thumbhash(TEST_IMAGE)
    assert h1 == h2


def test_compute_thumbhash_custom_size():
    """Custom size parameter should still produce valid hash."""
    from thumbnail.thumbhash_util import compute_thumbhash

    if not TEST_IMAGE or not os.path.exists(TEST_IMAGE):
        pytest.skip("Set TEST_IMAGE_PATH to a JPEG file")
    result = compute_thumbhash(TEST_IMAGE, size=50)
    assert isinstance(result, bytes)
    assert 15 <= len(result) <= 25
