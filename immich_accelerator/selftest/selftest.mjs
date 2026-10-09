// Self-test: does every kind of work the accelerator does still produce the
// expected output on this Immich?
//
// Runs with the worker's environment (every shim preloaded, the ffmpeg
// wrapper on FFMPEG_PATH) and with the current directory set to the Immich
// server's dist/, as the body of `node --input-type=module -e`. Bare imports
// therefore resolve exactly as Immich's own do, and each check drives Immich's
// own code (MediaRepository, MetadataRepository, its ffmpeg command builders)
// on a tiny fixture. An upstream change to how Immich does the work then shows
// up as a named failure, not as jobs failing in production (#191).
//
// A check that cannot reach Immich's code at all (it moved) is a harness
// error: a warning on a user's machine, a failure in the CI canary.
//
// argv: <server_dir> <fixtures_dir> [skip,list]
// Prints one line: SELFTEST {"checks": [{name, ok, detail, harness?}]}
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { pathToFileURL } from 'node:url';

const [serverDir, fixturesDir, skipList = ''] = process.argv.slice(1);
const skip = new Set(skipList.split(',').filter(Boolean));
const fixture = (name) => path.join(fixturesDir, name);
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'immich-accel-selftest-'));
const checks = [];

class HarnessError extends Error {}

async function check(name, fn) {
    if (skip.has(name)) return;
    try {
        checks.push({ name, ok: true, detail: String(await fn()) });
    } catch (e) {
        const msg = String((e && e.message) || e).split('\n').slice(0, 3).join(' | ');
        checks.push({ name, ok: false, detail: msg, harness: e instanceof HarnessError });
    }
}

function expect(cond, msg) {
    if (!cond) throw new Error(msg);
}

// Immich's own modules, loaded the way its worker loads them.
async function immich(rel) {
    try {
        return await import(pathToFileURL(path.join(serverDir, 'dist', rel)).href);
    } catch (e) {
        throw new HarnessError(`cannot load Immich's ${rel}: ${(e && e.message) || e}`);
    }
}

const quietLogger = () => new Proxy({}, { get: () => () => false });
const cache = {};
async function repo(rel, cls) {
    if (!cache[cls]) {
        const mod = await immich(rel);
        try {
            cache[cls] = new mod[cls](quietLogger());
        } catch (e) {
            throw new HarnessError(`cannot construct Immich's ${cls}: ${(e && e.message) || e}`);
        }
    }
    return cache[cls];
}
const media = () => repo('repositories/media.repository.js', 'MediaRepository');

async function decodeSize(file) {
    const m = await media();
    if (typeof m.decodeImage !== 'function') {
        // Older Immich: no decodeImage. Decode through the same bare import
        // Immich's files use, which still proves the shims reach it.
        const { default: sharp } = await import('sharp');
        const meta = await sharp(await sharp(file).png().toBuffer()).metadata();
        return `${meta.width}x${meta.height}`;
    }
    const { info } = await m.decodeImage(file, {
        colorspace: 'srgb', processInvalidImages: false, size: 1440, edits: [],
    });
    expect(info && info.width, 'decodeImage returned no image');
    return `${info.width}x${info.height}`;
}

// --- Images ----------------------------------------------------------------

await check('jpeg_thumbnail', async () => {
    const size = await decodeSize(fixture('face.jpg'));
    expect(size === '192x240', `decoded ${size}, expected 192x240`);
    return size;
});

// Sharp's bundled libvips has no HEVC decoder: this only works through the
// HEIC shim.
await check('heic_thumbnail', async () => {
    let size;
    try {
        size = await decodeSize(fixture('iphone.heic'));
    } catch (e) {
        if (e instanceof HarnessError) throw e;
        throw new Error(`HEVC HEIC did not decode, so the HEIC shim did not run: ${(e && e.message) || e}`);
    }
    expect(size === '96x64', `decoded ${size}, expected 96x64`);
    return size;
});

// The native macOS build, not the WebAssembly fallback (works, ~3x slower).
await check('sharp_native', async () => {
    const { default: sharp } = await import('sharp');
    expect(!sharp.versions.emscripten,
        'sharp is running its WebAssembly fallback, not the native macOS build');
    return `libvips ${sharp.versions.vips}`;
});

// --- Metadata --------------------------------------------------------------

await check('metadata', async () => {
    const meta = await repo('repositories/metadata.repository.js', 'MetadataRepository');
    try {
        const tags = await meta.readTags(fixture('face.jpg'));
        expect(tags.Make === 'Accelerator' && tags.Model === 'Selftest',
            `camera read as ${tags.Make} ${tags.Model}`);
        const taken = String(tags.DateTimeOriginal || '');
        expect(taken.startsWith('2024-05-17T10:30:00'), `date taken read as "${taken}"`);
        return `${tags.Make} ${tags.Model}, ${taken.slice(0, 19)}`;
    } finally {
        await meta.teardown?.();
    }
});

// --- Video (ffmpeg through the accelerator's wrapper) ----------------------

// Immich's fluent-ffmpeg reads FFMPEG_PATH; it must be our VideoToolbox
// wrapper, and the wrapper must point at an ffmpeg that runs.
await check('video_wrapper', async () => {
    const wrapper = process.env.FFMPEG_PATH;
    expect(wrapper, 'FFMPEG_PATH is not set: Immich would use plain ffmpeg');
    const body = fs.readFileSync(wrapper, 'utf8');
    expect(body.includes('VideoToolbox ffmpeg wrapper'), `${wrapper} is not the accelerator's wrapper`);
    const real = (body.match(/^REAL_FFMPEG="([^"]+)"/m) || [])[1];
    expect(real, 'the wrapper names no real ffmpeg');
    const cp = await import('node:child_process');
    const r = cp.spawnSync(real, ['-hide_banner', '-version'], { encoding: 'utf8' });
    expect(r.status === 0, `${real} does not run`);
    return (r.stdout.split('\n')[0] || real).slice(0, 60);
});

let probed = null;
async function probe() {
    if (!probed) probed = await (await media()).probe(fixture('hdr.mp4'));
    return probed;
}

await check('video_probe', async () => {
    const { videoStreams, audioStreams } = await probe();
    expect(videoStreams.length === 1 && audioStreams.length === 1,
        `found ${videoStreams.length} video and ${audioStreams.length} audio streams`);
    const v = videoStreams[0];
    expect(v.codecName === 'hevc' && v.height === 108, `video read as ${v.codecName} ${v.width}x${v.height}`);
    expect(String(v.pixelFormat).includes('10'), `pixel format read as ${v.pixelFormat}`);
    return `${v.codecName} ${v.width}x${v.height} ${v.pixelFormat}`;
});

async function ffmpegDefaults() {
    const { defaults } = await immich('dtos/config.dto.js');
    const { ThumbnailConfig, BaseConfig } = await immich('utils/media.js');
    const { TranscodeTarget, ColorTransfer } = await immich('enum.js');
    if (!defaults || !defaults.ffmpeg || !ThumbnailConfig || !BaseConfig || !TranscodeTarget || !ColorTransfer) {
        throw new HarnessError("Immich's ffmpeg config or command builders moved");
    }
    return { defaults, ThumbnailConfig, BaseConfig, TranscodeTarget, ColorTransfer };
}

// Immich's MediaService makes a video's preview this way: its ThumbnailConfig
// builds the ffmpeg command, MediaRepository.transcode runs it.
await check('video_thumbnail', async () => {
    const { defaults, ThumbnailConfig, TranscodeTarget } = await ffmpegDefaults();
    const { videoStreams, format } = await probe();
    const out = path.join(work, 'video-preview.jpeg');
    const options = ThumbnailConfig.create({ ...defaults.ffmpeg, targetResolution: '96' })
        .getCommand(TranscodeTarget.Video, videoStreams[0], undefined, format);
    await (await media()).transcode(fixture('hdr.mp4'), out, options);
    const size = await decodeSize(out);
    return `preview ${size}`;
});

// A full transcode with Immich's default settings, forced to run: the HDR
// source must come out as SDR H.264 with its audio.
await check('video_transcode', async () => {
    const { defaults, BaseConfig, TranscodeTarget, ColorTransfer } = await ffmpegDefaults();
    const { videoStreams, audioStreams } = await probe();
    expect(videoStreams[0].colorTransfer === ColorTransfer.Smpte2084, 'the fixture did not read as HDR');
    const out = path.join(work, 'transcode.mp4');
    const command = BaseConfig.create({ ...defaults.ffmpeg }, [])
        .getCommand(TranscodeTarget.All, videoStreams[0], audioStreams[0]);
    await (await media()).transcode(fixture('hdr.mp4'), out, command);
    const result = await (await media()).probe(out);
    const v = result.videoStreams[0];
    expect(v, 'transcode produced no video stream');
    expect(v.codecName === defaults.ffmpeg.targetVideoCodec,
        `video is ${v.codecName}, expected ${defaults.ffmpeg.targetVideoCodec}`);
    expect(v.colorTransfer !== ColorTransfer.Smpte2084, 'output is still HDR: tone-mapping did not run');
    expect(result.audioStreams.length === 1, 'audio was dropped');
    return `${v.codecName} ${v.width}x${v.height}, HDR to ${ColorTransfer[v.colorTransfer] || v.colorTransfer}`;
});

// --- Backups (pg_dump shim) ------------------------------------------------

// Immich spawns the Linux client path and pipes through `gzip --rsyncable`;
// the shim must send the first to Homebrew's pg_dump and make the second work.
await check('pg_dump', async () => {
    const cp = await import('node:child_process');
    expect(cp.default.__immichAccelPatched && cp.spawnSync === cp.default.spawnSync,
        "Immich's child_process imports are not patched");
    const v = cp.spawnSync('/usr/lib/postgresql/16/bin/pg_dump', ['--version'], { encoding: 'utf8' });
    expect(v.status === 0 && /pg_dump/.test(v.stdout), `pg_dump did not run: ${v.error || v.stderr || v.status}`);
    const input = Buffer.from('immich backup '.repeat(64));
    const z = cp.spawnSync('gzip', ['--rsyncable', '-c'], { input });
    expect(z.status === 0, `gzip --rsyncable failed: ${z.stderr}`);
    expect(zlib.gunzipSync(z.stdout).equals(input), 'gzip output does not round-trip');
    return v.stdout.trim();
});

// --- Job retry -------------------------------------------------------------

// The bullmq Immich imports must be patched, without help from
// @nestjs/bullmq also require()ing it.
await check('job_retry', async () => {
    const { Queue } = await import('bullmq');
    expect(Queue && Queue.prototype.add.__immichAccelJobRetry, "Immich's bullmq import is not patched");
    return 'patched';
});

fs.rmSync(work, { recursive: true, force: true });
console.log('SELFTEST ' + JSON.stringify({ checks }));
process.exit(0);
