#!/usr/bin/env python3
"""
immich-thumbnail-metal: GPU-accelerated thumbnail generation for Immich on Apple Silicon.

Pre-generates thumbnails using Core Image (Metal GPU) and writes them directly
to the Immich upload directory + database, bypassing Sharp/libvips CPU processing.

Architecture:
  1. Poll Postgres for assets with thumbhash IS NULL (IMAGE type first)
  2. Read original from shared filesystem  
  3. Resize via Core Image (Metal backend) → preview (1440px JPEG) + thumbnail (250px WebP)
  4. Compute thumbhash
  5. Write files + update DB
"""

import os
import sys
import time
import hashlib
import logging
import struct
import io
from pathlib import Path

# PyObjC imports for Core Image
import objc
from Foundation import NSData, NSURL, NSNumber
from Quartz import (
    CIImage, CIFilter, CIContext, 
    kCIFormatRGBA8, kCGInterpolationHigh,
    CGRectMake, CGSizeMake
)
from AppKit import NSBitmapImageRep, NSJPEGFileType, NSPNGFileType
from CoreGraphics import (
    CGColorSpaceCreateWithName, kCGColorSpaceSRGB,
    CGImageDestinationCreateWithURL, CGImageDestinationAddImage, 
    CGImageDestinationFinalize
)
import ImageIO

logging.basicConfig(level=logging.INFO, format="%(asctime)s [thumbnail] %(message)s")
log = logging.getLogger("thumbnail")

# Config
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "immich")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "postgres")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/Users/elp/docker/immich/upload")
PHOTOS_DIR = os.environ.get("PHOTOS_DIR", "/nas/Pictures")
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "20"))
PREVIEW_SIZE = 1440
THUMBNAIL_SIZE = 250
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))

# Path translation (same as ffmpeg proxy)
PATH_MAP = [
    ("/usr/src/app/upload/", UPLOAD_DIR + "/"),
    ("/mnt/photos/", PHOTOS_DIR + "/"),
]

def translate_path(p):
    for container_prefix, host_prefix in PATH_MAP:
        if p.startswith(container_prefix):
            return host_prefix + p[len(container_prefix):]
    return p

def resize_with_core_image(input_path, output_path, max_dimension, format="jpeg", quality=0.8):
    """Resize an image using Core Image (Metal GPU acceleration)."""
    url = NSURL.fileURLWithPath_(input_path)
    ci_image = CIImage.imageWithContentsOfURL_(url)
    if ci_image is None:
        raise ValueError(f"Cannot load image: {input_path}")
    
    # Get original dimensions
    extent = ci_image.extent()
    orig_w = extent.size.width
    orig_h = extent.size.height
    
    if orig_w == 0 or orig_h == 0:
        raise ValueError(f"Zero dimension image: {input_path}")
    
    # Calculate scale to fit within max_dimension
    scale = min(max_dimension / orig_w, max_dimension / orig_h)
    if scale >= 1.0:
        scale = 1.0  # Don't upscale
    
    # Apply Lanczos scale filter
    scale_filter = CIFilter.filterWithName_("CILanczosScaleTransform")
    scale_filter.setDefaults()
    scale_filter.setValue_forKey_(ci_image, "inputImage")
    scale_filter.setValue_forKey_(NSNumber.numberWithFloat_(scale), "inputScale")
    scale_filter.setValue_forKey_(NSNumber.numberWithFloat_(1.0), "inputAspectRatio")
    
    output_image = scale_filter.outputImage()
    if output_image is None:
        raise ValueError("Scale filter produced no output")
    
    # Create Metal-backed context for GPU rendering
    context = CIContext.contextWithOptions_({"kCIContextUseSoftwareRenderer": False})
    
    # Render to CGImage
    output_extent = output_image.extent()
    cg_image = context.createCGImage_fromRect_(output_image, output_extent)
    if cg_image is None:
        raise ValueError("Failed to render CGImage")
    
    # Write to file
    out_url = NSURL.fileURLWithPath_(output_path)
    
    if format == "webp":
        # Use ImageIO for WebP
        dest = ImageIO.CGImageDestinationCreateWithURL(out_url, "public.webp", 1, None)
        if dest:
            props = {ImageIO.kCGImageDestinationLossyCompressionQuality: quality}
            ImageIO.CGImageDestinationAddImage(dest, cg_image, props)
            ImageIO.CGImageDestinationFinalize(dest)
        else:
            raise ValueError("Cannot create WebP destination")
    else:
        # JPEG via ImageIO
        dest = ImageIO.CGImageDestinationCreateWithURL(out_url, "public.jpeg", 1, None)
        if dest:
            props = {ImageIO.kCGImageDestinationLossyCompressionQuality: quality}
            ImageIO.CGImageDestinationAddImage(dest, cg_image, props)
            ImageIO.CGImageDestinationFinalize(dest)
        else:
            raise ValueError("Cannot create JPEG destination")
    
    return True

def generate_thumbnails_for_asset(asset_id, original_path, user_id):
    """Generate preview + thumbnail for a single asset."""
    host_path = translate_path(original_path)
    
    if not os.path.exists(host_path):
        log.warning(f"  File not found: {host_path}")
        return False
    
    # Determine output paths (matches Immich's directory structure)
    # thumbs/{user_id}/{id[0:2]}/{id[2:4]}/{id}_preview.jpeg
    # thumbs/{user_id}/{id[0:2]}/{id[2:4]}/{id}_thumbnail.webp
    id_clean = asset_id.replace("-", "")
    sub_dir = os.path.join(UPLOAD_DIR, "thumbs", user_id, id_clean[:2], id_clean[2:4])
    os.makedirs(sub_dir, exist_ok=True)
    
    preview_path = os.path.join(sub_dir, f"{asset_id}_preview.jpeg")
    thumb_path = os.path.join(sub_dir, f"{asset_id}_thumbnail.webp")
    
    try:
        # Generate preview (1440px, JPEG)
        resize_with_core_image(host_path, preview_path, PREVIEW_SIZE, "jpeg", 0.80)
        
        # Generate thumbnail (250px, WebP)
        resize_with_core_image(host_path, thumb_path, THUMBNAIL_SIZE, "webp", 0.75)
        
        return True
    except Exception as e:
        log.error(f"  Failed: {e}")
        return False

def main():
    import psycopg2
    
    log.info(f"Starting thumbnail service")
    log.info(f"DB: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    log.info(f"Upload: {UPLOAD_DIR}")
    log.info(f"Photos: {PHOTOS_DIR}")
    log.info(f"Batch size: {BATCH_SIZE}")
    
    while True:
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                user=DB_USER, password=DB_PASS
            )
            conn.autocommit = True
            cur = conn.cursor()
            
            # Get IMAGE assets without thumbnails (prioritize images over videos)
            cur.execute("""
                SELECT a.id, a."originalPath", a."ownerId"::text
                FROM asset a
                WHERE a.thumbhash IS NULL
                  AND a.type = 'IMAGE'
                  AND a."deletedAt" IS NULL
                ORDER BY a."createdAt" DESC
                LIMIT %s
            """, (BATCH_SIZE,))
            
            rows = cur.fetchall()
            
            if not rows:
                # No images left, try videos (thumbnail = frame extract, handled by ffmpeg)
                log.info(f"No image assets pending. Sleeping {POLL_INTERVAL}s...")
                time.sleep(POLL_INTERVAL)
                conn.close()
                continue
            
            log.info(f"Processing {len(rows)} images...")
            
            success = 0
            for asset_id, original_path, user_id in rows:
                if generate_thumbnails_for_asset(asset_id, original_path, user_id):
                    # Update the asset_file table with the new thumbnail paths
                    # Immich checks for file existence, not just DB entries
                    # The thumbhash update tells Immich it's done
                    # For now, just generate the files — Immich's own job will
                    # find them and update the DB on next pass
                    success += 1
                    
            log.info(f"  Generated {success}/{len(rows)} thumbnails")
            conn.close()
            
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(10)
        
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
