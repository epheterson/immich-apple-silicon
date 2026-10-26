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

const { Transform } = require('stream');
const { pipeline } = require('stream/promises');

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
        // trims peak memory and tmp I/O for no pixel change. Level 1, because at
        // the default level deflate took far longer than the decode (24MP HEIC:
        // 0.37s to decode and rotate, 3.9s to deflate; level 1 ~0.5s for a ~20%
        // larger file). The magic bytes are unaffected, so isTiff still gates
        // it. sips (fallback) has no equivalent option and is left plain.
        bin: VIPS,
        args: (input, out) => ['autorot', input, `${out}[compression=deflate,level=1]`],
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

// libheif reads a HEIC through vips in tiny pieces: ~30,000 read(2) calls for
// a 3MB photo, most of them one byte. On local disk the page cache absorbs
// them; on an SMB mount each one is a network round trip, which is where the
// 20+ second NAS decodes come from. Copying the file sequentially first takes
// a fraction of a second. HEIC only: RAW can be large and needs its sidecars
// next to it. The cap is enforced while streaming; 0 turns staging off.
const stageMaxBytes = Number((process.env.IMMICH_ACCELERATOR_HEIC_STAGE_MAX_BYTES || '').trim() || NaN);
const HEIC_STAGE_MAX_BYTES = Number.isSafeInteger(stageMaxBytes) && stageMaxBytes >= 0
    ? stageMaxBytes : 128 * 1024 * 1024;

// A worker killed mid-decode leaves its copy and its TIFF behind. Clear any
// over an hour old at startup, in the background; a live decode is never
// that old (the budget is 120 s).
fs.promises.readdir(os.tmpdir()).then((names) => {
    for (const name of names) {
        if (!name.startsWith('iaa-heic-')) continue;
        const stale = path.join(os.tmpdir(), name);
        fs.promises.stat(stale).then((st) => {
            if (Date.now() - st.mtimeMs > 3600000) {
                return fs.promises.rm(stale, { recursive: true, force: true });
            }
        }).catch(warnStaleCleanup);
    }
}, warnStaleCleanup);
function warnStaleCleanup(e) {
    if (e.code === 'ENOENT') return; // already gone
    process.stderr.write(`[immich-accelerator] clearing old HEIC staging failed: ${e.message}\n`);
}

function removeStagedHeic(dir) {
    try { fs.rmSync(dir, { recursive: true, force: true }); }
    catch (e) {
        process.stderr.write(`[immich-accelerator] HEIC staging cleanup failed: ${e.message}\n`);
    }
}

async function stageHeic(input, deadline) {
    let dir;
    let output;
    let timer;
    let complete = false;
    try {
        const remaining = deadline - Date.now();
        if (remaining <= 0) throw new Error('decode deadline reached');
        const controller = new AbortController();
        // A read stuck on a hung mount never lets the streams close, so the
        // pipeline would never settle and the decode slot never come back.
        // Stop waiting at the deadline; the stuck read is left to finish.
        const timedOut = new Promise((_resolve, reject) => {
            timer = setTimeout(() => {
                controller.abort();
                reject(new Error('staging ran out of decode budget'));
            }, remaining);
        });
        timedOut.catch(() => {});
        const inTime = (work) => {
            work.catch(() => {});
            return Promise.race([work, timedOut]);
        };
        // Reading cap + 1 bytes of a file already known to be too big would
        // spend the decode budget on a copy that gets thrown away.
        if ((await inTime(fs.promises.stat(input))).size > HEIC_STAGE_MAX_BYTES) {
            throw new Error('larger than the staging cap');
        }
        dir = fs.mkdtempSync(path.join(os.tmpdir(), 'iaa-heic-input-'));
        const file = path.join(dir, 'input.heic');
        let bytes = 0;
        const limit = new Transform({
            transform(chunk, _encoding, callback) {
                bytes += chunk.length;
                callback(bytes > HEIC_STAGE_MAX_BYTES
                    ? new Error('HEIC staging byte limit exceeded') : null, chunk);
            },
        });
        output = fs.createWriteStream(file, { flags: 'wx', mode: 0o600 });
        await inTime(pipeline(
            // end is inclusive: read at most cap + 1 to detect oversize, even
            // if the source grows continually; never write that extra byte.
            fs.createReadStream(input, { end: HEIC_STAGE_MAX_BYTES }), limit, output,
            { signal: controller.signal }
        ));
        complete = true;
        return { dir, file };
    } catch (e) {
        process.stderr.write(
            `[immich-accelerator] HEIC staging failed for ${input}: ${e.message}; using original input\n`
        );
        return null;
    } finally {
        clearTimeout(timer);
        if (!complete) {
            if (output) output.destroy();
            if (dir) removeStagedHeic(dir);
        }
    }
}

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

// Async execFile lets BullMQ run several decodes concurrently. Reading a HEIC
// straight off an SMB share, that was slower, not faster: 4 concurrent vips
// decodes took 37s+ (still not done) vs 20.7s for one alone, because libheif's
// tiny reads each wait on the NAS. HEICs are now staged to local disk first
// (stageHeic), and with that, decoding 3 at a time finished a batch about 2x
// faster than 1 on an M4. Past 3 the gain is small and memory keeps climbing,
// since every decoder holds a full frame. Immich's thumbnail job concurrency
// is the real ceiling, so a higher value here only matters up to that.
// A queued decode gives up rather than waiting forever; see acquireDecodeSlot.
const DECODE_QUEUE_TIMEOUT_MS =
    Number(process.env.IMMICH_ACCELERATOR_HEIC_QUEUE_TIMEOUT_MS) || 180000;
const decodeConcurrency = Number(process.env.IMMICH_ACCELERATOR_HEIC_DECODE_CONCURRENCY);
// Zero, a negative or a fraction would leave no whole slot and stall every decode.
const DECODE_CONCURRENCY = Number.isSafeInteger(decodeConcurrency) && decodeConcurrency >= 1
    ? decodeConcurrency : 3;
let activeDecodes = 0;
const decodeWaitQueue = [];
function acquireDecodeSlot() {
    if (activeDecodes < DECODE_CONCURRENCY) {
        activeDecodes += 1;
        return Promise.resolve();
    }
    // Bounded, because an unbounded wait is how one stuck decoder stops every
    // HEIC and RAW thumbnail on the machine. execFile's timeout only sends
    // SIGTERM, and a vips blocked in uninterruptible I/O on a stalled SMB
    // mount (precisely the NAS case this is for) ignores it and never exits:
    // its promise never settles, the finally never runs, and the slot it holds
    // is gone for the life of the worker; once every slot is lost that way,
    // nothing decodes again.
    // Before this rewrite the decode was synchronous with a hard 30s ceiling,
    // so that could not happen; giving up the slot restores that guarantee.
    return new Promise((resolve, reject) => {
        const waiter = { resolve, done: false };
        const timer = setTimeout(() => {
            if (waiter.done) return;
            waiter.done = true;
            const i = decodeWaitQueue.indexOf(waiter);
            if (i !== -1) decodeWaitQueue.splice(i, 1);
            reject(new Error(
                `waited over ${DECODE_QUEUE_TIMEOUT_MS}ms for a decode slot`));
        }, DECODE_QUEUE_TIMEOUT_MS);
        waiter.fire = () => {
            if (waiter.done) return false;
            waiter.done = true;
            clearTimeout(timer);
            resolve();
            return true;
        };
        decodeWaitQueue.push(waiter);
    });
}
function releaseDecodeSlot() {
    // Skip waiters that already timed out, or the slot is handed to nobody.
    while (decodeWaitQueue.length) {
        const next = decodeWaitQueue.shift();
        if (next.fire && next.fire()) return;
    }
    activeDecodes -= 1;
}

// Decode a HEIC file to a lossless TIFF buffer, trying each decoder in
// preference order until one yields a valid TIFF. Resolves to null if none
// succeed (caller falls back to real Sharp).
async function decodeToBuffer(input) {
    try {
        await acquireDecodeSlot();
    } catch (e) {
        // Never held the slot, so must not release one. Falling back to real
        // Sharp is the same thing every other decode failure here does.
        process.stderr.write(`[immich-accelerator] ${input}: ${e.message}; falling back to Sharp\n`);
        return null;
    }
    try {
        return await decodeToBufferUnthrottled(input);
    } finally {
        releaseDecodeSlot();
    }
}

// Immich opens each HEIC original twice per thumbnail job: decodeImage() for
// the pixels, then getImageMetadata() to check for transparency. Under Docker
// that second call only reads the file header. Here metadata() is a terminal
// call, so it ran the whole decoder chain a second time on the same file.
// Give the next open of the same unchanged file (path, mtime, size) the decode
// that already ran, once, within a short window, then let it go so the frame
// isn't kept alive. A failed decode is never reused. HEIC only: RAW isn't
// transparency-capable, so nothing would claim its frame.
const DECODE_REUSE_MS = 10000;
// A caller that never opens the file again would otherwise leave every frame
// here for the full window; keep only the most recent few.
const DECODE_REUSE_MAX = 8;
const recentDecodes = new Map();
function decodeOnceForReuse(input) {
    if (isRawPath(input)) return decodeToBuffer(input);
    let key;
    try {
        const st = fs.statSync(input);
        key = `${st.mtimeMs}:${st.size}`;
    } catch (_e) {
        return decodeToBuffer(input);
    }
    const hit = recentDecodes.get(input);
    if (hit) {
        recentDecodes.delete(input);
        clearTimeout(hit.timer);
        if (hit.key === key) return hit.promise;
    }
    const entry = { key, promise: decodeToBuffer(input), timer: null };
    if (recentDecodes.size >= DECODE_REUSE_MAX) {
        const [oldest, old] = recentDecodes.entries().next().value;
        clearTimeout(old.timer);
        recentDecodes.delete(oldest);
    }
    recentDecodes.set(input, entry);
    const forget = () => {
        if (recentDecodes.get(input) === entry) recentDecodes.delete(input);
    };
    entry.promise.then((buf) => {
        if (recentDecodes.get(input) !== entry) return;
        if (buf === null) return forget();
        entry.timer = setTimeout(forget, DECODE_REUSE_MS);
        // An idle worker must still be able to exit.
        entry.timer.unref();
    }, forget);
    return entry.promise;
}

async function decodeToBufferUnthrottled(input) {
    const deadline = Date.now() + DECODE_BUDGET_MS;
    // Staging holds the same semaphore slot and spends the same total budget
    // as the decoders. RAW keeps the original path for sidecar resolution.
    // Only HEVC HEIC and RAW reach here (wrapSharp), so not RAW means HEIC.
    const staged = HEIC_STAGE_MAX_BYTES > 0 && !isRawPath(input)
        ? await stageHeic(input, deadline) : null;
    try {
        return await decodeWithDeadline(input, deadline, staged ? staged.file : input);
    } finally {
        if (staged) removeStagedHeic(staged.dir);
    }
}

async function decodeWithDeadline(input, deadline, decoderInput) {
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
            await execFileAsync(dec.bin, dec.args(decoderInput, tmp), {
                timeout: remaining,
                env: decoderEnv(),
                // The synchronous version ignored stdout and piped stderr.
                // execFile buffers both into a 1MB maxBuffer by default and
                // KILLS the child on overflow, so a chatty decoder on a large
                // RAW would fail a decode that was working. We only care
                // whether the output file is a valid TIFF.
                maxBuffer: 8 * 1024 * 1024,
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
    if (attempted === 0 && Date.now() >= deadline) {
        process.stderr.write(
            `[immich-accelerator] HEIC/RAW decode budget exhausted for ${input}; falling back to Sharp\n`
        );
    } else if (attempted === 0) {
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

// Sharp instances are also Duplex streams. This proxy cannot be one: the
// decode it stands in for is async, and a stream has to exist synchronously.
// Immich 3.0.2 never streams on the HEIC path, but if a future version does,
// failing here by name beats returning a proxy where a stream was expected and
// letting it fail somewhere unrecognisable.
const STREAM_PROPS = new Set([
    'pipe', 'on', 'once', 'off', 'emit', 'read', 'write', 'end', 'destroy',
    'removeListener', 'addListener', 'unpipe', 'pause', 'resume',
]);

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
    const decoded = decodeOnceForReuse(input).then((buf) => (buf !== null ? buf : input));
    // Never an unhandled rejection: nothing awaits `decoded` until a terminal
    // method runs, and if no terminal ever runs (an exception between sharp()
    // and .toBuffer(), say) an unattached rejection would take the worker down
    // on Node 15+, where that is fatal by default.
    decoded.catch(() => {});
    // Built per proxy so clone() can hand out an independent copy.
    const build = (calls) => {
      const proxy = new Proxy(function () {}, {
        get(_target, prop) {
            if (typeof prop === 'symbol' || NON_CHAIN_PROPS.has(prop)) return undefined;
            // clone() exists to fork a pipeline into two outputs. Recorded as
            // an ordinary chain call it returned the same proxy, so both
            // branches appended to one list and each output got the other's
            // operations too.
            if (prop === 'clone') return () => build([...calls]);
            if (STREAM_PROPS.has(prop)) {
                return () => {
                    throw new Error(
                        `immich-accelerator HEIC shim: sharp().${prop}() is not ` +
                        'supported on HEIC/RAW input, which this shim decodes ' +
                        'ahead of Sharp. Use toBuffer() or toFile(), or report ' +
                        'this at github.com/epheterson/immich-apple-silicon.'
                    );
                };
            }
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
    };
    return build([]);
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

// One wrapper per Sharp module object, shared by the CommonJS and ESM paths
// below. Keyed by object because the two paths can load different copies.
const wrappers = new WeakMap();
function wrappedSharp(realSharp) {
    if (realSharp.__heicShimWrapped) return realSharp;
    let w = wrappers.get(realSharp);
    if (!w) {
        w = wrapSharp(realSharp);
        wrappers.set(realSharp, w);
    }
    return w;
}

// Intercept `require('sharp')` and return the wrapped factory. The preload
// runs before Immich's entrypoint, so this is installed before Sharp is first
// required.
const Module = require('module');
const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
    const mod = origLoad.apply(this, arguments);
    if (request === 'sharp' && mod) return wrappedSharp(mod);
    return mod;
};

// Immich 3.3 is ESM (`import sharp from 'sharp'`), and Sharp ships an ESM
// build, so that import never reaches Module._load (#191). Resolve the import
// exactly as Node would, then hand Immich a bridge module that imports that
// same URL and exports wrappedSharp() of it. Nothing here depends on which
// formats Sharp ships. registerHooks exists on Node 22.15+ (Homebrew's
// node@22 has it).
const ESM_BRIDGE = require('url').pathToFileURL(
    require('path').join(__dirname, 'sharp_esm_bridge.mjs')
);
if (typeof Module.registerHooks === 'function') {
    Module.registerHooks({
        resolve(specifier, context, nextResolve) {
            const resolved = nextResolve(specifier, context);
            const conditions = context.conditions || [];
            if (specifier !== 'sharp' || !conditions.includes('import')) return resolved;
            const url = new URL(ESM_BRIDGE);
            url.searchParams.set('real', resolved.url);
            return { url: url.href, format: 'module', shortCircuit: true };
        },
    });
} else {
    process.stderr.write(
        '[immich-accelerator] warning: this Node has no module.registerHooks; ' +
        'HEIC decoding will not reach ESM imports of sharp (Immich 3.3+)\n'
    );
}

// Loaded via `node --require`, where exports go unused. They exist so the
// chain-replay behaviour can be driven directly from a test rather than
// inferred from thumbnails, and so the ESM bridge shares the one wrapper.
module.exports = { wrapSharp, lazyDecodedSharp, wrappedSharp };
