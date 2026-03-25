"""Tests for thumbnail.db -- ThumbnailDB."""

import os

import pytest
from thumbnail.db import ThumbnailDB


# Use env vars for test configuration; defaults are generic placeholders.
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/test-upload")
PHOTOS_DIR = os.environ.get("PHOTOS_DIR", "/tmp/test-photos")
DB_PASS = os.environ.get("DB_PASS", "testpass")


# Shared fixture
@pytest.fixture
def db():
    return ThumbnailDB(
        host="localhost", port=5432, dbname="immich",
        user="postgres", password=DB_PASS,
        upload_dir=UPLOAD_DIR,
        photos_dir=PHOTOS_DIR,
    )


# --- Path translation (no DB needed) ---

def test_translate_path_photos(db):
    assert db.translate_path("/mnt/photos/iCloud/test.jpg") == PHOTOS_DIR + "/iCloud/test.jpg"


def test_translate_path_upload(db):
    assert db.translate_path("/usr/src/app/upload/thumbs/abc") == UPLOAD_DIR + "/thumbs/abc"


def test_translate_path_passthrough(db):
    assert db.translate_path("/some/other/path") == "/some/other/path"


def test_container_path_photos(db):
    assert db.container_path(PHOTOS_DIR + "/iCloud/test.jpg") == "/mnt/photos/iCloud/test.jpg"


def test_container_path_upload(db):
    assert db.container_path(UPLOAD_DIR + "/thumbs/abc") == "/usr/src/app/upload/thumbs/abc"


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
