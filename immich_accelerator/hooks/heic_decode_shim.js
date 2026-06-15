// heic_decode_shim.js
//
// Runtime interposition for HEIC decoding in Immich's media pipeline.
//
// The native worker uses Sharp's prebuilt libvips (8.17.x), whose bundled
// libheif is compiled WITHOUT an HEVC decoder — it supports AVIF only. So
// every HEIC original (the default iPhone format) fails to decode with a
// "bad seek" / "compression format has not been built in" error, and no
// thumbnail is produced (issue #62 follow-up). Docker Immich isn't affected
// because it builds libvips with libde265.
//
// Neither off-the-shelf option works on macOS: forcing Sharp onto Homebrew's
// system libvips reintroduces the #44 JPEG-detection regression AND still
// can't decode normal tiled iPhone HEICs (libheif's iref-reference security
// limit rejects them, 45 refs > 16). Apple's own ImageIO (`sips`) decodes
// every HEIC — tiled or single — natively and fast, with nothing to build.
//
// Fix: preload this module via `NODE_OPTIONS=--require ...` and wrap the
// `sharp` module export. When Sharp is handed a HEVC-family HEIC file path,
// we transcode it to a lossless TIFF with `sips` and feed that buffer to the
// real Sharp instead. Everything else (JPEG, PNG, AVIF, buffers, streams)
// passes through untouched. Immich's source on disk is never modified — same
// interposition pattern as the ffmpeg wrapper and the pg_dump shim.
//
// AVIF is deliberately NOT routed here: Sharp's bundled libheif decodes AVIF
// (via aom) fine, so we leave those alone.

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

const SIPS = process.env.IMMICH_ACCELERATOR_SIPS || '/usr/bin/sips';

// Brands that specifically imply HEVC-coded HEIF — what the bundled libheif
// cannot decode. We match against the major brand AND the compatible-brands
// list so a file with a generic major brand (e.g. `mif1`) but `heic` in its
// compatible brands is still caught. AVIF (major `avif`/`avis`, compatible
// `av01`) carries none of these, so it is left for Sharp's own (aom) decoder.
const HEVC_BRANDS = new Set([
    'heic', 'heix', 'heim', 'heis', 'hevc', 'hevx', 'hevm', 'hevs',
]);

// Read the `ftyp` box brands: the major brand (bytes 8..12) plus the
// compatible brands that follow (4 bytes each from offset 16). The ftyp box
// is small, so a 64-byte read covers the major brand and ~12 compatible
// brands. Returns null for anything that isn't an ftyp-led file.
function readFtypBrands(filePath) {
    let fd;
    try {
        fd = fs.openSync(filePath, 'r');
        const buf = Buffer.alloc(64);
        const n = fs.readSync(fd, buf, 0, 64, 0);
        if (n < 12 || buf.toString('latin1', 4, 8) !== 'ftyp') return null;
        const brands = [buf.toString('latin1', 8, 12).trim()];
        for (let off = 16; off + 4 <= n; off += 4) {
            const b = buf.toString('latin1', off, off + 4).trim();
            if (b) brands.push(b);
        }
        return brands;
    } catch (_e) {
        return null;
    } finally {
        if (fd !== undefined) {
            try { fs.closeSync(fd); } catch (_e) { /* ignore */ }
        }
    }
}

function isHevcHeicPath(input) {
    if (typeof input !== 'string' || input.length === 0) return false;
    const brands = readFtypBrands(input);
    return brands !== null && brands.some((b) => HEVC_BRANDS.has(b));
}

// Decode a HEIC file to a lossless TIFF buffer via Apple's ImageIO. Returns
// the buffer, or null if sips is unavailable or fails (caller falls back to
// the original input so behavior is never worse than without the shim).
function sipsDecodeToBuffer(input) {
    const tmp = path.join(
        os.tmpdir(),
        `iaa-heic-${process.pid}-${Date.now()}-${Math.floor(Math.random() * 1e9)}.tiff`
    );
    try {
        cp.execFileSync(SIPS, ['-s', 'format', 'tiff', input, '--out', tmp], {
            stdio: ['ignore', 'ignore', 'pipe'],
            // The decode runs synchronously in the sharp() constructor, so this
            // timeout caps how long a hung sips can stall the worker's event
            // loop. 30s is generous for a single image yet bounds the worst case.
            timeout: 30000,
        });
        return fs.readFileSync(tmp);
    } catch (e) {
        process.stderr.write(
            `[immich-accelerator] HEIC decode via sips failed for ${input}: ` +
            `${String((e && e.message) || e).split('\n')[0]}\n`
        );
        return null;
    } finally {
        try { fs.unlinkSync(tmp); } catch (_e) { /* ignore */ }
    }
}

// Wrap the real Sharp factory so HEVC-HEIC paths are pre-decoded.
function wrapSharp(realSharp) {
    function sharp(input, options) {
        if (isHevcHeicPath(input)) {
            const buf = sipsDecodeToBuffer(input);
            if (buf !== null) {
                return realSharp(buf, options);
            }
            // Fall through to the real Sharp so the user still gets Immich's
            // normal (libheif) error path rather than a silent difference.
        }
        return realSharp(input, options);
    }
    // Carry over the static API surface (cache, concurrency, format,
    // versions, simd, etc.). Sharp exposes these as own properties on the
    // factory function.
    Object.assign(sharp, realSharp);
    sharp.__heicShimWrapped = true;
    return sharp;
}

// Intercept `require('sharp')` and return the wrapped factory. The preload
// runs before Immich's entrypoint, so this is installed before Sharp is first
// required.
const Module = require('module');
const origLoad = Module._load;
let wrapped = null;
Module._load = function (request, parent, isMain) {
    const mod = origLoad.apply(this, arguments);
    if (request === 'sharp' && mod && !mod.__heicShimWrapped) {
        if (wrapped === null) wrapped = wrapSharp(mod);
        return wrapped;
    }
    return mod;
};
