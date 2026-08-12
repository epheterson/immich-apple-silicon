// heic_decode_shim.js
//
// Runtime interposition for HEIC and camera-RAW decoding in Immich's media
// pipeline.
//
// The native worker uses Sharp's prebuilt libvips (8.17.x). Two format families
// it cannot decode on macOS, both of which Docker Immich handles fine:
//
//   1. HEVC-HEIC (the default iPhone format): Sharp's bundled libheif is
//      compiled WITHOUT an HEVC decoder (AVIF only), so every HEIC original
//      fails with "bad seek" / "compression format has not been built in" and
//      no thumbnail is produced (issue #62). Docker builds libvips with
//      libde265, so it is unaffected.
//   2. Camera RAW (Canon CR2/CR3, Nikon NEF, Sony ARW, Adobe DNG, and friends):
//      Sharp's bundled libtiff/libjpeg lack old-style-JPEG support and it has no
//      dcraw/libraw loader, so a CR2 dies in AssetGenerateThumbnails with
//      "tiff2vips: Old-style JPEG compression support is not configured"
//      (issue #99). Docker's libvips (fuller libtiff plus libraw) reads it.
//
// Fix: preload this module via `NODE_OPTIONS=--require ...` and wrap the
// `sharp` module export. When Sharp is handed a HEVC-HEIC or camera-RAW file
// path, we transcode it to a lossless TIFF with Homebrew's fuller libvips and
// feed that buffer to the real Sharp instead. Everything else (JPEG, PNG, AVIF,
// ordinary TIFF, buffers, streams) passes through untouched. Immich's source on
// disk is never modified, the same interposition pattern as the ffmpeg wrapper
// and pg_dump shim.
//
// Decoder preference (first that yields a valid TIFF wins):
//   1. libheif via `vips autorot` (Homebrew libvips built WITH libde265) — a
//      lossless TIFF with EXIF orientation baked into the pixels. This is what
//      Docker Immich uses (libvips + libde265), so it is the closest output
//      match, it works fully HEADLESS, and `vips` is a formula dependency so it
//      is always present.
//   2. Apple ImageIO via `sips` — also a lossless, orientation-baked TIFF. Fast
//      and native, but it needs a logged-in GUI (Aqua/WindowServer) session, so
//      in a headless launchd context it SILENTLY produces empty output. Last
//      resort, only useful on a logged-in desktop.
//
// Both decoders BAKE orientation, so the oriented result is identical whichever
// runs — a thumbnail (and any ML box computed from it) never depends on the
// host's decoder set. vips is preferred precisely because sips cannot be relied
// on in a background service.
//
// AVIF is deliberately NOT routed here: Sharp's bundled libheif decodes AVIF
// (via aom) fine, so we leave those alone.

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');
const util = require('util');

const execFileAsync = util.promisify(cp.execFile);

// Resolve a decoder binary. An explicit env override is honored VERBATIM: an
// operator forcing a specific build should fail loudly (a bad path errors on
// use and falls through to the next decoder, logged) rather than be silently
// ignored. Otherwise the fixed Homebrew prefixes (Apple Silicon + Intel). No
// PATH lookup, so only a trusted, fixed location is ever executed.
function findBin(envVar, candidates) {
    const override = process.env[envVar];
    if (override) return override;
    for (const c of candidates) {
        if (fs.existsSync(c)) return c;
    }
    return null;
}

const VIPS = findBin('IMMICH_ACCELERATOR_VIPS', [
    '/opt/homebrew/bin/vips',
    '/usr/local/bin/vips',
]);
const SIPS = findBin('IMMICH_ACCELERATOR_SIPS', ['/usr/bin/sips']);

// Decoders in preference order. Both BAKE EXIF orientation into the pixels
// (vips `autorot`; sips applies orientation on convert) and write a lossless
// TIFF, so the oriented result is identical no matter which decoder ran — a
// thumbnail (and any ML box computed from it) never depends on the host's
// decoder set. vips (Homebrew libvips built with libde265) is primary: it works
// headless and matches Docker Immich's own libvips output. sips (Apple ImageIO)
// is a last-resort desktop fallback that needs a logged-in GUI session, so it
// silently produces nothing in a headless launchd context.
const DECODERS = [
    {
        name: 'vips',
        // Write the intermediate TIFF with lossless deflate compression. Sharp
        // ingests the whole file into a Node Buffer, and a camera RAW decodes to
        // a large frame (tens of MB uncompressed, ~40% smaller deflated), so this
        // trims peak memory and tmp I/O for no pixel change and negligible CPU
        // (the decode dominates). The magic bytes are unaffected, so isTiff still
        // gates it. sips (fallback) has no equivalent option and is left plain.
        bin: VIPS,
        args: (input, out) => ['autorot', input, `${out}[compression=deflate]`],
    },
    {
        name: 'sips',
        bin: SIPS,
        args: (input, out) => ['-s', 'format', 'tiff', input, '--out', out],
    },
];

// One-time startup line so the worker log shows the shim is active and which
// HEIC decoders it resolved (invaluable when HEIC thumbnails misbehave).
process.stderr.write(
    '[immich-accelerator] heic/raw decode shim active ' +
    `(decoders: vips=${VIPS || 'no'}, sips=${SIPS || 'no'})\n`
);

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

// Camera-RAW extensions that Sharp's bundled libvips cannot decode on macOS
// (its libtiff/libjpeg lack old-style-JPEG support and it has no dcraw/libraw
// loader) but Homebrew libvips can. RAW is matched by EXTENSION, not magic:
// most RAW containers are TIFF-based, so their magic bytes don't reliably
// separate them from an ordinary TIFF that Sharp already handles. Routing these
// through `vips autorot` mirrors Docker Immich's own libvips+libraw path; if a
// file with one of these extensions is not actually decodable RAW, the decoder
// fails the TIFF check and we fall back to the real Sharp (no worse than now).
// Generic `.raw` is intentionally omitted: it is too ambiguous (also raw audio,
// disk images, etc.) to intercept safely.
const RAW_EXTS = new Set([
    'cr2', 'cr3', 'crw',          // Canon
    'nef', 'nrw',                 // Nikon
    'arw', 'sr2', 'srf',          // Sony
    'raf',                        // Fujifilm
    'orf', 'ori',                 // Olympus
    'rw2',                        // Panasonic
    'pef',                        // Pentax
    'dng',                        // Adobe / generic
    'rwl',                        // Leica
    'dcr', 'kdc', 'k25',          // Kodak
    'mrw',                        // Minolta
    'x3f',                        // Sigma
    '3fr', 'fff',                 // Hasselblad
    'mos', 'iiq',                 // Leaf / Phase One
    'erf',                        // Epson
    'mef',                        // Mamiya
    'mdc',                        // Minolta / Agfa
    'ari', 'cap',                 // Arri / others
]);

function isRawPath(input) {
    if (typeof input !== 'string' || input.length === 0) return false;
    const dot = input.lastIndexOf('.');
    if (dot < 0) return false;
    return RAW_EXTS.has(input.slice(dot + 1).toLowerCase());
}

// Decode now runs via async execFile, so a slow decode can't block the event
// loop or starve BullMQ's lock renewal — this is a hang-safety ceiling, not
// an event-loop-protection lever. Raised well above 8s because a NAS-backed
// library (e.g. SMB) can take 20+ seconds for a normal read, not a hang.
const DECODE_BUDGET_MS = Number(process.env.IMMICH_ACCELERATOR_HEIC_DECODE_BUDGET_MS) || 120000;

// A real TIFF starts with "II*\0" (little-endian) or "MM\0*" (big-endian). Both
// our decoders emit TIFF; requiring the magic rejects an empty or half-written
// file (e.g. a GUI-less sips) so the next decoder gets a turn instead of feeding
// Sharp garbage.
function isTiff(buf) {
    return (
        buf.length >= 4 &&
        ((buf[0] === 0x49 && buf[1] === 0x49 && buf[2] === 0x2a && buf[3] === 0x00) ||
            (buf[0] === 0x4d && buf[1] === 0x4d && buf[2] === 0x00 && buf[3] === 0x2a))
    );
}

// Sharp's prebuilt libvips exports VIPSHOME into the process environment,
// pointing at the build tree it was compiled in (a GitHub-runner path baked into
// the binary, e.g. /Users/runner/work/sharp-libvips/...). When we spawn the
// Homebrew `vips`, it inherits that VIPSHOME and hunts for its loader modules
// (libheif for HEIC, etc.) under that bogus directory, so the decode fails with
// "not a known file format" and silently falls back to sips (which needs a GUI
// session, so it produces nothing headless). If VIPSHOME points at a directory
// that does not exist, drop it from the child's env so the spawned vips uses its
// own compiled-in module path. A real, existing VIPSHOME (an intentional
// operator override) is left untouched.
function decoderEnv() {
    const home = process.env.VIPSHOME;
    if (home && !fs.existsSync(home)) {
        const env = { ...process.env };
        delete env.VIPSHOME;
        return env;
    }
    return process.env;
}

// Async execFile lets BullMQ run several decodes concurrently, but a modest
// NAS degrades under concurrent reads instead of parallelizing them —
// measured, 4 concurrent vips decodes took 37s+ (still not done) vs 20.7s for
// one alone. Cap concurrent decoder processes; default 1 matches what
// execFileSync gave for free before.
const DECODE_CONCURRENCY = Number(process.env.IMMICH_ACCELERATOR_HEIC_DECODE_CONCURRENCY) || 1;
let activeDecodes = 0;
const decodeWaitQueue = [];
function acquireDecodeSlot() {
    if (activeDecodes < DECODE_CONCURRENCY) {
        activeDecodes += 1;
        return Promise.resolve();
    }
    return new Promise((resolve) => decodeWaitQueue.push(resolve));
}
function releaseDecodeSlot() {
    const next = decodeWaitQueue.shift();
    if (next) {
        next();
    } else {
        activeDecodes -= 1;
    }
}

// Decode a HEIC file to a lossless TIFF buffer, trying each decoder in
// preference order until one yields a valid TIFF. Resolves to null if none
// succeed (caller falls back to real Sharp).
async function decodeToBuffer(input) {
    await acquireDecodeSlot();
    try {
        return await decodeToBufferUnthrottled(input);
    } finally {
        releaseDecodeSlot();
    }
}

async function decodeToBufferUnthrottled(input) {
    const deadline = Date.now() + DECODE_BUDGET_MS;
    let attempted = 0;
    for (const dec of DECODERS) {
        if (!dec.bin) continue;
        const remaining = deadline - Date.now();
        if (remaining <= 0) break;
        attempted += 1;
        const tmp = path.join(
            os.tmpdir(),
            `iaa-heic-${process.pid}-${Date.now()}-${Math.floor(Math.random() * 1e9)}.tiff`
        );
        try {
            await execFileAsync(dec.bin, dec.args(input, tmp), {
                timeout: remaining,
                env: decoderEnv(),
            });
            const buf = fs.readFileSync(tmp);
            if (isTiff(buf)) return buf;
            process.stderr.write(
                `[immich-accelerator] HEIC decode via ${dec.name} produced no ` +
                `usable output for ${input}; trying next decoder.\n`
            );
        } catch (e) {
            process.stderr.write(
                `[immich-accelerator] HEIC decode via ${dec.name} failed for ` +
                `${input}: ${String((e && e.message) || e).split('\n')[0]}\n`
            );
        } finally {
            try { fs.unlinkSync(tmp); } catch (_e) { /* ignore */ }
        }
    }
    if (attempted === 0) {
        process.stderr.write(
            '[immich-accelerator] No HEIC/RAW decoder found (install vips for ' +
            'headless HEIC and camera-RAW support); falling back to Sharp.\n'
        );
    }
    return null;
}

// then/catch/finally must not look like chain methods, or code that
// duck-types "is this a Promise" would misbehave.
const NON_CHAIN_PROPS = new Set(['then', 'catch', 'finally', 'constructor']);

// These trigger real pixel decode and return a Promise; everything else
// Immich calls (rotate, resize, toFormat, ...) is a synchronous setter.
const TERMINAL_ASYNC_METHODS = new Set(['toBuffer', 'toFile', 'metadata', 'stats']);

// sharp() must return synchronously and support chaining even though decode
// is now async. Records synchronous chain calls and replays them once decode
// resolves; a terminal call (toBuffer etc.) awaits it first.
function lazyDecodedSharp(input, options, realSharp) {
    // Resolve to the decoded SOURCE, not to a Sharp instance. Each terminal
    // call then builds its own instance and applies the chain to that.
    //
    // Sharing one instance replayed the whole recorded chain onto it every
    // time a terminal method ran, and real Sharp chain methods return `this`,
    // so a second output re-applied rotate/resize on top of the first. Immich
    // 3.0.2 happens not to reuse a pipeline (every output calls sharp() again),
    // so nothing triggers it today — but this shim wraps whatever Immich the
    // user is running, and an upstream change to reuse one pipeline would
    // silently double-transform their thumbnails with no error anywhere.
    const decoded = decodeToBuffer(input).then((buf) => (buf !== null ? buf : input));
    // Never an unhandled rejection: nothing awaits `decoded` until a terminal
    // method runs, and if no terminal ever runs (an exception between sharp()
    // and .toBuffer(), say) an unattached rejection would take the worker down
    // on Node 15+, where that is fatal by default.
    decoded.catch(() => {});
    const calls = [];
    const proxy = new Proxy(function () {}, {
        get(_target, prop) {
            if (typeof prop === 'symbol' || NON_CHAIN_PROPS.has(prop)) return undefined;
            if (TERMINAL_ASYNC_METHODS.has(prop)) {
                return async (...args) => {
                    let instance = realSharp(await decoded, options);
                    for (const [method, methodArgs] of calls) {
                        instance = instance[method](...methodArgs);
                    }
                    return instance[prop](...args);
                };
            }
            return (...args) => {
                calls.push([prop, args]);
                return proxy;
            };
        },
    });
    return proxy;
}

// Wrap the real Sharp factory so HEVC-HEIC paths are pre-decoded.
function wrapSharp(realSharp) {
    function sharp(input, options) {
        // isRawPath is a pure string check; isHevcHeicPath reads the ftyp box.
        // Test the cheap one first so RAW paths route with no filesystem I/O.
        if (isRawPath(input) || isHevcHeicPath(input)) {
            return lazyDecodedSharp(input, options, realSharp);
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
