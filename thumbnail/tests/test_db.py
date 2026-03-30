"""Tests for thumbnail.db -- ThumbnailDB."""

import os
from unittest.mock import MagicMock, patch

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


# --- Custom container paths (remote Docker setups) ---

@pytest.fixture
def custom_db():
    """DB with non-standard container paths (e.g., Synology NAS running Docker)."""
    return ThumbnailDB(
        host="localhost", port=5432, dbname="immich",
        user="postgres", password=DB_PASS,
        upload_dir="/Volumes/docker/immich/library",
        photos_dir="/Volumes/photo",
        container_upload="/data/upload",
        container_photos="/mnt/media/Syno",
    )


def test_custom_translate_upload(custom_db):
    assert custom_db.translate_path("/data/upload/abc/def.JPG") == "/Volumes/docker/immich/library/abc/def.JPG"


def test_custom_translate_photos(custom_db):
    assert custom_db.translate_path("/mnt/media/Syno/2012/DSC_3918.JPG") == "/Volumes/photo/2012/DSC_3918.JPG"


def test_custom_container_path_upload(custom_db):
    assert custom_db.container_path("/Volumes/docker/immich/library/abc/def.JPG") == "/data/upload/abc/def.JPG"


def test_custom_container_path_photos(custom_db):
    assert custom_db.container_path("/Volumes/photo/2012/DSC_3918.JPG") == "/mnt/media/Syno/2012/DSC_3918.JPG"


def test_custom_roundtrip(custom_db):
    original = "/mnt/media/Syno/2024/vacation/IMG_001.heic"
    assert custom_db.container_path(custom_db.translate_path(original)) == original


def test_default_paths_unchanged(db):
    """Verify defaults still work when custom paths aren't provided."""
    assert db.container_upload == "/usr/src/app/upload/"
    assert db.container_photos == "/mnt/photos/"


# --- Mocked database tests (no Postgres needed) ---

def test_mark_complete_batch_calls_correct_sql(db):
    """Batch write should execute 3 SQL statements per asset + 1 commit."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch.object(db, "_connect", return_value=mock_conn):
        db.mark_complete_batch([
            {"asset_id": "aaa", "preview_path": "/p/aaa_preview.jpeg",
             "thumb_path": "/t/aaa_thumbnail.webp", "thumbhash": b"\x01\x02"},
            {"asset_id": "bbb", "preview_path": "/p/bbb_preview.jpeg",
             "thumb_path": "/t/bbb_thumbnail.webp", "thumbhash": b"\x03\x04"},
        ])

    # 3 execute calls per asset (2 upserts + 1 thumbhash update)
    assert mock_cursor.execute.call_count == 6
    # Single commit for the whole batch
    mock_conn.commit.assert_called_once()


def test_mark_complete_batch_empty():
    """Empty batch should be a no-op — no DB calls."""
    mock_conn = MagicMock()
    db_inst = ThumbnailDB(
        host="x", port=5432, dbname="x", user="x", password="x",
        upload_dir="/tmp", photos_dir="/tmp",
    )
    with patch.object(db_inst, "_connect", return_value=mock_conn):
        db_inst.mark_complete_batch([])
    mock_conn.cursor.assert_not_called()
    mock_conn.commit.assert_not_called()


def test_mark_complete_batch_rollback_on_error(db):
    """On SQL error, should rollback and re-raise."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.side_effect = Exception("SQL boom")
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch.object(db, "_connect", return_value=mock_conn):
        with pytest.raises(Exception, match="SQL boom"):
            db.mark_complete_batch([
                {"asset_id": "aaa", "preview_path": "/p", "thumb_path": "/t", "thumbhash": b"\x01"},
            ])
    mock_conn.rollback.assert_called_once()


def test_mark_complete_single_still_works(db):
    """Original per-asset mark_complete should still work."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with patch.object(db, "_connect", return_value=mock_conn):
        db.mark_complete("aaa", "/preview", "/thumb", b"\x01\x02")
    assert mock_cursor.execute.call_count == 3
    mock_conn.commit.assert_called_once()


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
