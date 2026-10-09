// Self-test: does each shim still reach the Immich code it exists for?
//
// Runs with the worker's NODE_OPTIONS (every shim preloaded) and with the
// current directory set to the Immich server's dist/, as the body of
// `node --input-type=module -e`. Bare imports here therefore resolve exactly
// as Immich's own `import ... from 'x'` does, and an upstream change to how
// Immich loads a module shows up as a failed check instead of a shim that
// says "active" while doing nothing (#191).
//
// argv: <server_dir> <fixture.heic> [skip,list]
// Prints one line: SELFTEST {"checks": [{name, ok, detail}]}
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [serverDir, fixture, skipList = ''] = process.argv.slice(1);
const skip = new Set(skipList.split(',').filter(Boolean));
const checks = [];

async function check(name, fn) {
    if (skip.has(name)) return;
    try {
        checks.push({ name, ok: true, detail: String(await fn()) });
    } catch (e) {
        const msg = String((e && e.message) || e).split('\n').slice(0, 3).join(' | ');
        checks.push({ name, ok: false, detail: msg });
    }
}

// HEIC shim: Immich's own thumbnail decode of an HEVC HEIC. Sharp's bundled
// libvips has no HEVC decoder, so this only succeeds through the shim.
await check('heic_thumbnail', async () => {
    const file = path.join(serverDir, 'dist', 'repositories', 'media.repository.js');
    const { MediaRepository } = await import(pathToFileURL(file).href);
    if (typeof MediaRepository !== 'function') {
        throw new Error(`no MediaRepository in ${file}`);
    }
    const logger = new Proxy({}, { get: () => () => false });
    const repo = new MediaRepository(logger);
    if (typeof repo.decodeImage !== 'function') {
        // Older Immich: no decodeImage. Decode through the same bare import
        // Immich's files use, which still proves the shim reaches it.
        const { default: sharp } = await import('sharp');
        const meta = await sharp(await sharp(fixture).png().toBuffer()).metadata();
        return `${meta.width}x${meta.height} (via sharp)`;
    }
    const { info } = await repo.decodeImage(fixture, {
        colorspace: 'srgb', processInvalidImages: false, size: 1440, edits: [],
    });
    if (!info || !info.width) throw new Error('decodeImage returned no image');
    return `${info.width}x${info.height}`;
});

// Sharp itself: the native macOS build, not the WebAssembly fallback it uses
// when the darwin binary is missing (works, but about 3x slower).
await check('sharp_native', async () => {
    const { default: sharp } = await import('sharp');
    if (sharp.versions.emscripten) {
        throw new Error('sharp is running its WebAssembly fallback, not the native macOS build');
    }
    return `libvips ${sharp.versions.vips}`;
});

// Job retry shim: the bullmq Immich imports must be the patched one, without
// help from @nestjs/bullmq also require()ing it.
await check('job_retry', async () => {
    const { Queue } = await import('bullmq');
    if (!Queue || !Queue.prototype.add.__immichAccelJobRetry) {
        throw new Error("Immich's bullmq import is not patched");
    }
    return 'patched';
});

// pg_dump shim: Immich imports spawn/execFile by name from node:child_process.
await check('pg_dump', async () => {
    const cp = await import('node:child_process');
    if (!cp.default.__immichAccelPatched || cp.spawn !== cp.default.spawn) {
        throw new Error("Immich's child_process imports are not patched");
    }
    return 'patched';
});

console.log('SELFTEST ' + JSON.stringify({ checks }));
process.exit(0);
