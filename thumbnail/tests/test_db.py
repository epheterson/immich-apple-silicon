"""Tests for thumbnail.db — ThumbnailDB."""

import pytest
from thumbnail.db import ThumbnailDB


# Shared fixture
@pytest.fixture
def db():
    return ThumbnailDB(
        host="localhost", port=5432, dbname="immich",
        user="postgres", password="postgres",
        upload_dir="/Users/elp/docker/immich/upload",
        photos_dir="/nas/Pictures",
    )


# --- Path translation (no DB needed) ---

def test_translate_path_photos(db):
    assert db.translate_path("/mnt/photos/iCloud/test.jpg") == "/nas/Pictures/iCloud/test.jpg"


def test_translate_path_upload(db):
    assert db.translate_path("/usr/src/app/upload/thumbs/abc") == "/Users/elp/docker/immich/upload/thumbs/abc"


def test_translate_path_passthrough(db):
    assert db.translate_path("/some/other/path") == "/some/other/path"


def test_container_path_photos(db):
    assert db.container_path("/nas/Pictures/iCloud/test.jpg") == "/mnt/photos/iCloud/test.jpg"


def test_container_path_upload(db):
    assert db.container_path("/Users/elp/docker/immich/upload/thumbs/abc") == "/usr/src/app/upload/thumbs/abc"


def test_container_path_passthrough(db):
    assert db.container_path("/some/other/path") == "/some/other/path"


def test_roundtrip_photos(db):
    original = "/mnt/photos/iCloud/2024/IMG_001.heic"
    assert db.container_path(db.translate_path(original)) == original


def test_roundtrip_upload(db):
    original = "/usr/src/app/upload/thumbs/owner123/asset456.webp"
    assert db.container_path(db.translate_path(original)) == original


# --- Database tests (need live Postgres) ---

@pytest.mark.db
def test_get_pending_assets(db):
    assets = db.get_pending_assets(limit=5, asset_type="IMAGE")
    assert isinstance(assets, list)
    for a in assets:
        assert "id" in a
        assert "originalPath" in a
        assert "ownerId" in a


@pytest.mark.db
def test_get_stats(db):
    stats = db.get_stats()
    assert "total" in stats
    assert "done" in stats
    assert "pending_images" in stats
    assert "pending_videos" in stats
    assert stats["total"] >= 0
