"""Entry point for the Immich Apple Silicon thumbnail worker.

Usage:
    python -m thumbnail

Configuration via environment variables:
    DB_HOST         Postgres host         (default: localhost)
    DB_PORT         Postgres port         (default: 5432)
    DB_NAME         Database name         (default: immich)
    DB_USER         Database user         (default: postgres)
    DB_PASS         Database password     (default: postgres)
    UPLOAD_DIR      Immich upload mount   (default: /Users/elp/docker/immich/upload)
    PHOTOS_DIR      External photos mount (default: /nas/Pictures)
    BATCH_SIZE      Assets per poll       (default: 20)
    POLL_INTERVAL   Seconds between polls (default: 5)
"""

import logging
import os
import sys

from thumbnail.db import ThumbnailDB
from thumbnail.worker import ThumbnailWorker


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    db_host = os.environ.get("DB_HOST", "localhost")
    db_port = int(os.environ.get("DB_PORT", "5432"))
    db_name = os.environ.get("DB_NAME", "immich")
    db_user = os.environ.get("DB_USER", "postgres")
    db_pass = os.environ.get("DB_PASS", "postgres")
    upload_dir = os.environ.get("UPLOAD_DIR", "/Users/elp/docker/immich/upload")
    photos_dir = os.environ.get("PHOTOS_DIR", "/nas/Pictures")
    batch_size = int(os.environ.get("BATCH_SIZE", "20"))
    poll_interval = int(os.environ.get("POLL_INTERVAL", "5"))

    db = ThumbnailDB(
        host=db_host, port=db_port, dbname=db_name,
        user=db_user, password=db_pass,
        upload_dir=upload_dir, photos_dir=photos_dir,
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
