"""Integration test -- process one real asset end-to-end.

Requires:
    - Live Postgres with Immich DB on localhost:5432
    - External photos mount (PHOTOS_DIR env var)
    - At least one pending IMAGE asset
"""

import os

import pytest

from thumbnail.db import ThumbnailDB
from thumbnail.worker import ThumbnailWorker

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "")
PHOTOS_DIR = os.environ.get("PHOTOS_DIR", "")
DB_PASS = os.environ.get("DB_PASS", "")


@pytest.fixture
def db():
    if not UPLOAD_DIR or not PHOTOS_DIR or not DB_PASS:
        pytest.skip("Set UPLOAD_DIR, PHOTOS_DIR, and DB_PASS env vars for integration tests")
    return ThumbnailDB(
        host="localhost", port=5432, dbname="immich",
        user="postgres", password=DB_PASS,
        upload_dir=UPLOAD_DIR,
        photos_dir=PHOTOS_DIR,
    )


@pytest.mark.db
def test_full_pipeline_one_asset(db):
    """Pick one pending asset, generate thumbnails, verify everything."""
    # 1. Get a pending asset
    pending = db.get_pending_assets(limit=1, asset_type="IMAGE")
    if not pending:
        pytest.skip("No pending IMAGE assets")
    asset = pending[0]
    asset_id = asset["id"]
    owner_id = asset["ownerId"]
    original_path = asset["originalPath"]

    print(f"\n  Asset:    {asset_id}")
    print(f"  Owner:    {owner_id}")
    print(f"  Original: {original_path}")

    # Verify source is reachable
    host_path = db.translate_path(original_path)
    assert os.path.isfile(host_path), f"Source not found: {host_path}"

    # 2. Process it
    worker = ThumbnailWorker(db=db, upload_dir=UPLOAD_DIR)

    result = worker.process_asset(asset_id, original_path, owner_id)

    assert worker.processed == 1, f"Expected 1 processed, got {worker.processed}"
    assert worker.errors == 0, f"Expected 0 errors, got {worker.errors}"
    assert result is not None, "process_asset returned None (failure)"

    # Write to DB (process_asset now returns a dict for batch writes)
    db.mark_complete_batch([result])

    # 3. Verify output files exist
    stripped = asset_id.replace("-", "")
    out_dir = os.path.join(UPLOAD_DIR, "thumbs", owner_id, stripped[0:2], stripped[2:4])
    preview_path = os.path.join(out_dir, f"{asset_id}_preview.jpeg")
    thumb_path = os.path.join(out_dir, f"{asset_id}_thumbnail.webp")

    assert os.path.isfile(preview_path), f"Preview not found: {preview_path}"
    assert os.path.isfile(thumb_path), f"Thumbnail not found: {thumb_path}"

    preview_size = os.path.getsize(preview_path)
    thumb_size = os.path.getsize(thumb_path)
    assert preview_size > 0, "Preview is empty"
    assert thumb_size > 0, "Thumbnail is empty"

    print(f"  Preview:  {preview_path} ({preview_size:,} bytes)")
    print(f"  Thumb:    {thumb_path} ({thumb_size:,} bytes)")

    # 4. Verify DB was updated
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(
        host="localhost", port=5432, dbname="immich",
        user="postgres", password=DB_PASS,
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check thumbhash
            cur.execute("SELECT thumbhash FROM asset WHERE id = %s", (asset_id,))
            row = cur.fetchone()
            assert row is not None, "Asset not found in DB"
            assert row["thumbhash"] is not None, "thumbhash still NULL after processing"
            print(f"  Thumbhash: {len(bytes(row['thumbhash']))} bytes")

            # Check asset_file rows
            cur.execute(
                'SELECT type, path FROM asset_file WHERE "assetId" = %s ORDER BY type',
                (asset_id,),
            )
            files = cur.fetchall()
            types = {f["type"] for f in files}
            assert "preview" in types, "No preview row in asset_file"
            assert "thumbnail" in types, "No thumbnail row in asset_file"

            for f in files:
                print(f"  asset_file: {f['type']} -> {f['path']}")
    finally:
        conn.close()

    print(f"\n  SUCCESS -- asset {asset_id} fully processed")
