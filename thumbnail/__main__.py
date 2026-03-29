"""Entry point for the Immich Apple Silicon thumbnail worker.

Usage:
    python -m thumbnail

Configuration via environment variables:
    DB_HOST                Postgres host                   (default: localhost)
    DB_PORT                Postgres port                   (default: 5432)
    DB_NAME                Database name                   (default: immich)
    DB_USER                Database user                   (default: postgres)
    DB_PASS                Database password               (REQUIRED)
    UPLOAD_DIR             Immich upload mount on host      (REQUIRED)
    PHOTOS_DIR             External photos mount on host    (REQUIRED)
    CONTAINER_UPLOAD_PATH  Upload path inside Docker        (default: /usr/src/app/upload)
    CONTAINER_PHOTOS_PATH  Photos path inside Docker        (default: /mnt/photos)
    BATCH_SIZE             Assets per poll                  (default: 20)
    POLL_INTERVAL          Seconds between polls            (default: 5)
"""
from __future__ import annotations

import logging
import os
import sys

from thumbnail.db import ThumbnailDB
from thumbnail.worker import ThumbnailWorker


def main() -> None:
    """Configure logging, read env vars, and start the thumbnail worker loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # Required environment variables
    db_pass = os.environ.get("DB_PASS")
    if not db_pass:
        print("ERROR: DB_PASS environment variable required")
        sys.exit(1)

    upload_dir = os.environ.get("UPLOAD_DIR")
    if not upload_dir:
        print("ERROR: UPLOAD_DIR environment variable required")
        sys.exit(1)

    photos_dir = os.environ.get("PHOTOS_DIR")
    if not photos_dir:
        print("ERROR: PHOTOS_DIR environment variable required")
        sys.exit(1)

    db_host = os.environ.get("DB_HOST", "localhost")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_name = os.environ.get("DB_NAME", "immich")
    db_user = os.environ.get("DB_USER", "postgres")
    container_upload = os.environ.get("CONTAINER_UPLOAD_PATH", "")
    container_photos = os.environ.get("CONTAINER_PHOTOS_PATH", "")
    batch_size = int(os.environ.get("BATCH_SIZE", "20"))
    poll_interval = int(os.environ.get("POLL_INTERVAL", "5"))

    db = ThumbnailDB(
        host=db_host, port=db_port, dbname=db_name,
        user=db_user, password=db_pass,
        upload_dir=upload_dir, photos_dir=photos_dir,
        container_upload=container_upload, container_photos=container_photos,
    )

    # Quick stats before starting
    try:
        stats = db.get_stats()
        logging.getLogger(__name__).info(
            "DB stats — total: %d, done: %d, pending images: %d, pending videos: %d",
            stats["total"], stats["done"],
            stats["pending_images"], stats["pending_videos"],
        )
    except Exception as e:
        logging.getLogger(__name__).warning("Could not fetch stats: %s", e)

    worker = ThumbnailWorker(
        db=db, upload_dir=upload_dir,
        batch_size=batch_size, poll_interval=poll_interval,
    )
    worker.run()


if __name__ == "__main__":
    main()
