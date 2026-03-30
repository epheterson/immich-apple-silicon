"""Tests for the thumbnail worker loop — batch writes, prefetch, error handling.

Uses mocked DB and generate_all so no Postgres or macOS frameworks needed.
"""
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from thumbnail.db import ThumbnailDB
from thumbnail.worker import ThumbnailWorker


@pytest.fixture
def mock_db():
    """ThumbnailDB with all DB calls mocked out."""
    db = ThumbnailDB(
        host="localhost", port=5432, dbname="immich",
        user="postgres", password="test",
        upload_dir="/tmp/test-upload",
        photos_dir="/tmp/test-photos",
    )
    db.get_pending_assets = MagicMock(return_value=[])
    db.mark_complete = MagicMock()
    db.mark_complete_batch = MagicMock()
    return db


@pytest.fixture
def worker(mock_db):
    return ThumbnailWorker(db=mock_db, upload_dir="/tmp/test-upload", batch_size=5)


def _fake_asset(asset_id, path="/mnt/photos/test.jpg", owner="owner1"):
    return {"id": asset_id, "originalPath": path, "ownerId": owner}


# --- process_asset ---

def test_process_asset_returns_dict_on_success(worker):
    """Successful process_asset returns a result dict, not True."""
    with tempfile.NamedTemporaryFile(suffix=".jpg") as src:
        # Create a minimal JPEG
        from PIL import Image
        img = Image.new("RGB", (100, 100), "blue")
        img.save(src.name, "JPEG")

        with patch.object(worker.db, "translate_path", return_value=src.name):
            with patch("thumbnail.worker.generate_all",
                       return_value=(100, 100, 50, 50, b"\x01\x02\x03")):
                result = worker.process_asset("asset-1", "/mnt/photos/test.jpg", "owner-1")

    assert result is not None
    assert result["asset_id"] == "asset-1"
    assert result["thumbhash"] == b"\x01\x02\x03"
    assert "preview_path" in result
    assert "thumb_path" in result
    assert worker.processed == 1
    assert worker.errors == 0


def test_process_asset_returns_none_on_missing_file(worker):
    with patch.object(worker.db, "translate_path", return_value="/nonexistent/file.jpg"):
        result = worker.process_asset("asset-1", "/mnt/photos/missing.jpg", "owner-1")
    assert result is None
    assert worker.errors == 1


def test_process_asset_returns_none_on_generate_failure(worker):
    with tempfile.NamedTemporaryFile(suffix=".jpg") as src:
        from PIL import Image
        img = Image.new("RGB", (100, 100), "red")
        img.save(src.name, "JPEG")

        with patch.object(worker.db, "translate_path", return_value=src.name):
            with patch("thumbnail.worker.generate_all", side_effect=RuntimeError("GPU fail")):
                result = worker.process_asset("asset-1", "/mnt/photos/test.jpg", "owner-1")

    assert result is None
    assert worker.errors == 1


# --- Batch processing in run() ---

def test_batch_db_write_called_once_per_batch(worker):
    """mark_complete_batch called once with all results, not per-asset."""
    assets = [_fake_asset("a1"), _fake_asset("a2"), _fake_asset("a3")]
    fake_result = {"asset_id": "x", "preview_path": "/p", "thumb_path": "/t", "thumbhash": b"\x01"}

    # Return assets once, then empty (so loop exits after one batch)
    worker.db.get_pending_assets = MagicMock(side_effect=[assets, []])

    with patch.object(worker, "process_asset", return_value=fake_result):
        with patch.object(worker, "_available_memory_mb", return_value=9999):
            worker.db.get_pending_assets = MagicMock(side_effect=[assets] + [[] for _ in range(5)])

            # Patch time.sleep to not actually sleep, and stop after idle
            with patch("thumbnail.worker.time") as mock_time:
                mock_time.monotonic.return_value = 1.0
                mock_time.sleep.side_effect = lambda _: setattr(worker, '_running', False)
                worker.run()

    # Batch write should have been called exactly once with 3 results
    assert worker.db.mark_complete_batch.call_count == 1
    batch_arg = worker.db.mark_complete_batch.call_args[0][0]
    assert len(batch_arg) == 3


def test_batch_db_failure_triggers_backoff(worker):
    """If mark_complete_batch raises, batch failure counter increments."""
    assets = [_fake_asset("a1")]
    fake_result = {"asset_id": "a1", "preview_path": "/p", "thumb_path": "/t", "thumbhash": b"\x01"}

    worker.db.get_pending_assets = MagicMock(side_effect=[assets] + [[] for _ in range(5)])
    worker.db.mark_complete_batch = MagicMock(side_effect=Exception("DB down"))

    with patch.object(worker, "process_asset", return_value=fake_result):
        with patch.object(worker, "_available_memory_mb", return_value=9999):
            with patch("thumbnail.worker.time") as mock_time:
                mock_time.monotonic.return_value = 1.0
                mock_time.sleep.side_effect = lambda _: setattr(worker, '_running', False)
                worker.run()

    assert worker._consecutive_batch_failures >= 1


def test_all_assets_fail_triggers_batch_backoff(worker):
    """When every asset in a batch fails, batch failure counter increments."""
    assets = [_fake_asset("a1"), _fake_asset("a2")]

    worker.db.get_pending_assets = MagicMock(side_effect=[assets] + [[] for _ in range(5)])

    with patch.object(worker, "process_asset", return_value=None):  # all fail
        with patch.object(worker, "_available_memory_mb", return_value=9999):
            with patch("thumbnail.worker.time") as mock_time:
                mock_time.monotonic.return_value = 1.0
                mock_time.sleep.side_effect = lambda _: setattr(worker, '_running', False)
                worker.run()

    assert worker._consecutive_batch_failures >= 1
    worker.db.mark_complete_batch.assert_not_called()


# --- NFS prefetch ---

def test_prefetch_submits_next_file(worker):
    """Prefetch pool should be called for the next asset in the batch."""
    assets = [_fake_asset("a1", "/mnt/photos/1.jpg"),
              _fake_asset("a2", "/mnt/photos/2.jpg")]
    fake_result = {"asset_id": "x", "preview_path": "/p", "thumb_path": "/t", "thumbhash": b"\x01"}

    worker.db.get_pending_assets = MagicMock(side_effect=[assets] + [[] for _ in range(5)])

    with patch.object(worker, "process_asset", return_value=fake_result):
        with patch.object(worker, "_available_memory_mb", return_value=9999):
            with patch("thumbnail.worker._prefetch_pool") as mock_pool:
                with patch("thumbnail.worker.time") as mock_time:
                    mock_time.monotonic.return_value = 1.0
                    mock_time.sleep.side_effect = lambda _: setattr(worker, '_running', False)
                    worker.run()

    # Should have submitted a prefetch for the second asset
    prefetch_calls = [c for c in mock_pool.submit.call_args_list
                      if len(c[0]) >= 1 and callable(c[0][0])]
    assert len(prefetch_calls) >= 1


# --- Memory pressure ---

def test_low_memory_pauses(worker):
    """Worker should pause when available memory is low."""
    with patch.object(worker, "_available_memory_mb", return_value=100):
        with patch("thumbnail.worker.time") as mock_time:
            mock_time.sleep.side_effect = lambda _: setattr(worker, '_running', False)
            worker.run()

    # Should not have tried to fetch assets
    worker.db.get_pending_assets.assert_not_called()
