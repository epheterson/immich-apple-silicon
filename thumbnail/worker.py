"""Thumbnail worker — polls Immich DB and generates preview + thumbnail + thumbhash."""

import logging
import os
import signal
import time

from thumbnail.db import ThumbnailDB
from thumbnail.resize import resize_image
from thumbnail.thumbhash_util import compute_thumbhash

log = logging.getLogger(__name__)


class ThumbnailWorker:
    """Processes Immich assets that are missing thumbnails.

    Polls the database for pending assets, generates preview (1440px JPEG)
    and thumbnail (250px WebP) via Metal-accelerated resize, computes
    thumbhash, and writes results back to the DB.
    """

    def __init__(self, db: ThumbnailDB, upload_dir: str,
                 batch_size: int = 20, poll_interval: int = 5):
        self.db = db
        self.upload_dir = upload_dir.rstrip("/")
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.processed = 0
        self.errors = 0
        self._running = True

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        name = signal.Signals(signum).name
        log.info("Received %s — finishing current asset then stopping", name)
        self._running = False

    def _output_dir(self, owner_id: str, asset_id: str) -> str:
        """Build the output directory for an asset's thumbnails."""
        stripped = asset_id.replace("-", "")
        return os.path.join(
            self.upload_dir, "thumbs", owner_id,
            stripped[0:2], stripped[2:4],
        )

    def process_asset(self, asset_id: str, original_path: str, owner_id: str) -> None:
        """Generate preview + thumbnail + thumbhash for a single asset."""
        host_path = self.db.translate_path(original_path)

        if not os.path.isfile(host_path):
            log.error("Source file missing: %s (container: %s)", host_path, original_path)
            self.errors += 1
            return

        out_dir = self._output_dir(owner_id, asset_id)
        preview_path = os.path.join(out_dir, f"{asset_id}_preview.jpeg")
        thumb_path = os.path.join(out_dir, f"{asset_id}_thumbnail.webp")

        try:
            os.makedirs(out_dir, exist_ok=True)

            # Preview — 1440px JPEG
            pw, ph = resize_image(host_path, preview_path, max_dim=1440, format="jpeg", quality=80)
            log.debug("Preview %dx%d → %s", pw, ph, preview_path)

            # Thumbnail — 250px WebP
            tw, th = resize_image(host_path, thumb_path, max_dim=250, format="webp", quality=80)
            log.debug("Thumbnail %dx%d → %s", tw, th, thumb_path)

            # Thumbhash from the small WebP
            thumbhash = compute_thumbhash(thumb_path)

            # Container paths for DB
            preview_container = self.db.container_path(preview_path)
            thumb_container = self.db.container_path(thumb_path)

            # Write to DB
            self.db.mark_complete(
                asset_id=asset_id,
                owner_id=owner_id,
                preview_container_path=preview_container,
                thumb_container_path=thumb_container,
                thumbhash_bytes=thumbhash,
            )

            self.processed += 1
            log.info("OK %s (%dx%d preview, %dx%d thumb, %d-byte hash)",
                     asset_id, pw, ph, tw, th, len(thumbhash))

        except Exception:
            self.errors += 1
            log.exception("FAILED %s", asset_id)
            # Clean up partial output
            for path in (preview_path, thumb_path):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def run(self) -> None:
        """Main loop — poll for pending assets, process, repeat."""
        log.info("Worker started (batch=%d, poll=%ds, upload=%s)",
                 self.batch_size, self.poll_interval, self.upload_dir)

        batch_start = time.monotonic()

        while self._running:
            try:
                assets = self.db.get_pending_assets(limit=self.batch_size)
            except Exception:
                log.exception("DB query failed — retrying in %ds", self.poll_interval)
                time.sleep(self.poll_interval)
                continue

            if not assets:
                elapsed = time.monotonic() - batch_start
                if self.processed > 0:
                    rate = self.processed / max(elapsed, 0.001)
                    log.info("Idle — %d processed, %d errors, %.1f assets/sec",
                             self.processed, self.errors, rate)
                else:
                    log.info("Idle — no pending assets")
                time.sleep(self.poll_interval)
                continue

            for asset in assets:
                if not self._running:
                    break
                self.process_asset(
                    asset_id=asset["id"],
                    original_path=asset["originalPath"],
                    owner_id=asset["ownerId"],
                )

            # Log rate after each batch
            elapsed = time.monotonic() - batch_start
            rate = self.processed / max(elapsed, 0.001)
            log.info("Batch done — %d processed, %d errors, %.1f assets/sec",
                     self.processed, self.errors, rate)

        log.info("Worker stopped — %d processed, %d errors total",
                 self.processed, self.errors)
