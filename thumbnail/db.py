"""Database access layer for Immich thumbnail worker.

Queries the Immich Postgres database to find assets needing thumbnails,
and writes back preview/thumbnail paths + thumbhash when done.
"""
from __future__ import annotations

import psycopg2
import psycopg2.extensions
import psycopg2.extras


class ThumbnailDB:
    """Talks to the Immich Postgres database for thumbnail work."""

    # Default container paths used by Immich inside Docker
    DEFAULT_CONTAINER_UPLOAD = "/usr/src/app/upload/"
    DEFAULT_CONTAINER_PHOTOS = "/mnt/photos/"

    def __init__(self, host: str, port: int, dbname: str, user: str, password: str,
                 upload_dir: str, photos_dir: str,
                 container_upload: str = "", container_photos: str = ""):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self.upload_dir = upload_dir.rstrip("/") + "/"
        self.photos_dir = photos_dir.rstrip("/") + "/"
        self.container_upload = (container_upload or self.DEFAULT_CONTAINER_UPLOAD).rstrip("/") + "/"
        self.container_photos = (container_photos or self.DEFAULT_CONTAINER_PHOTOS).rstrip("/") + "/"
        self._conn = None

    def _connect(self) -> psycopg2.extensions.connection:
        """Return a psycopg2 connection, reusing an existing one if healthy."""
        if self._conn is not None:
            try:
                self._conn.cursor().execute("SELECT 1")
                return self._conn
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

        self._conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
        )
        return self._conn

    def translate_path(self, container_path: str) -> str:
        """Translate a container path to a host path.

        container_upload/... → upload_dir/...
        container_photos/... → photos_dir/...
        """
        if container_path.startswith(self.container_upload):
            return self.upload_dir + container_path[len(self.container_upload):]
        if container_path.startswith(self.container_photos):
            return self.photos_dir + container_path[len(self.container_photos):]
        return container_path

    def container_path(self, host_path: str) -> str:
        """Translate a host path back to a container path (reverse of translate_path)."""
        if host_path.startswith(self.upload_dir):
            return self.container_upload + host_path[len(self.upload_dir):]
        if host_path.startswith(self.photos_dir):
            return self.container_photos + host_path[len(self.photos_dir):]
        return host_path

    def get_pending_assets(self, limit: int = 20, asset_type: str = "IMAGE") -> list[dict]:
        """Return assets that need thumbnails (thumbhash IS NULL).

        Returns list of dicts with keys: id, originalPath, ownerId.
        """
        sql = """
            SELECT a.id, a."originalPath", a."ownerId"::text
            FROM asset a
            WHERE a.thumbhash IS NULL
              AND a.type = %s
              AND a."deletedAt" IS NULL
            ORDER BY a."createdAt" DESC
            LIMIT %s
        """
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (asset_type, limit))
                rows = cur.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            # Connection may be broken; reset it
            self._conn = None
            raise

    def mark_complete(self, asset_id: str,
                      preview_container_path: str, thumb_container_path: str,
                      thumbhash_bytes: bytes) -> None:
        """Mark an asset as having thumbnails generated.

        UPSERTs preview + thumbnail into asset_file, updates asset.thumbhash.
        """
        upsert_sql = """
            INSERT INTO asset_file ("assetId", type, path, "updateId", "isEdited", "isProgressive", "isTransparent")
            VALUES (%s, %s, %s, immich_uuid_v7(), false, false, false)
            ON CONFLICT ("assetId", type, "isEdited") DO UPDATE SET
                path = EXCLUDED.path, "updateId" = immich_uuid_v7(), "updatedAt" = now()
        """
        thumbhash_sql = """
            UPDATE asset SET thumbhash = %s, "updateId" = immich_uuid_v7() WHERE id = %s
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(upsert_sql, (asset_id, "preview", preview_container_path))
                cur.execute(upsert_sql, (asset_id, "thumbnail", thumb_container_path))
                cur.execute(thumbhash_sql, (psycopg2.Binary(thumbhash_bytes), asset_id))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._conn = None
            raise

    def mark_complete_batch(self, results: list[dict]) -> None:
        """Batch-write thumbnails for multiple assets in one transaction.

        Much faster than per-asset commits over the network — one commit
        for the entire batch instead of one per asset.

        Each result dict: {asset_id, preview_path, thumb_path, thumbhash}
        """
        if not results:
            return
        upsert_sql = """
            INSERT INTO asset_file ("assetId", type, path, "updateId", "isEdited", "isProgressive", "isTransparent")
            VALUES (%s, %s, %s, immich_uuid_v7(), false, false, false)
            ON CONFLICT ("assetId", type, "isEdited") DO UPDATE SET
                path = EXCLUDED.path, "updateId" = immich_uuid_v7(), "updatedAt" = now()
        """
        thumbhash_sql = """
            UPDATE asset SET thumbhash = %s, "updateId" = immich_uuid_v7() WHERE id = %s
        """
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                for r in results:
                    cur.execute(upsert_sql, (r["asset_id"], "preview", r["preview_path"]))
                    cur.execute(upsert_sql, (r["asset_id"], "thumbnail", r["thumb_path"]))
                    cur.execute(thumbhash_sql, (psycopg2.Binary(r["thumbhash"]), r["asset_id"]))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._conn = None
            raise

    def get_stats(self) -> dict:
        """Return thumbnail generation stats.

        Returns dict with keys: total, done, pending_images, pending_videos.
        """
        sql = """
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE thumbhash IS NOT NULL) AS done,
                count(*) FILTER (WHERE thumbhash IS NULL AND type = 'IMAGE' AND "deletedAt" IS NULL) AS pending_images,
                count(*) FILTER (WHERE thumbhash IS NULL AND type = 'VIDEO' AND "deletedAt" IS NULL) AS pending_videos
            FROM asset
        """
        conn = self._connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return dict(row)
        except Exception:
            self._conn = None
            raise
