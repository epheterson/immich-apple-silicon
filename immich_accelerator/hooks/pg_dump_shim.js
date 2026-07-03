// pg_dump_shim.js
//
// Runtime interposition for Immich's DatabaseBackupService. Two
// things go wrong when the backup runs on a native macOS worker:
//
// 1. Immich hardcodes `/usr/lib/postgresql/${version}/bin/pg_dump`
//    (and pg_dumpall, pg_restore, psql) in dist. Linux paths, SIP-
//    protected on macOS, no env-var escape hatch upstream.
//
// 2. Immich pipes pg_dump stdout through `gzip --rsyncable`.
//    Apple's BSD gzip does NOT support --rsyncable — it errors out
//    immediately with "unrecognized option", produces zero bytes,
//    and the upstream stream plumbing (repositories/process.repo.
//    spawnDuplexStream) does NOT check gzip's exit code, so the
//    pipeline resolves "cleanly" and Immich logs `Database Backup
//    Success` with a 0-byte file on disk. Issue #24 tail.
//
// Fix: preload this module via `NODE_OPTIONS=--require ...` and
// monkey-patch child_process spawn/spawnSync/execFile. For postgres
// client binaries, rewrite /usr/lib/postgresql/<ver>/bin/<x> to
// the Homebrew libpq path. For `gzip`, if --rsyncable is present
// AND Homebrew's GNU gzip is installed, route the call to
// /opt/homebrew/bin/gzip (which DOES support --rsyncable);
// otherwise strip --rsyncable and let BSD gzip handle it (the
// output is still a valid .gz, just not rsync-friendly).
//
// Immich's source files on disk are NEVER touched — the README's
// "unmodified" claim stays true. Same interposition pattern as the
// ffmpeg wrapper, just at the Node module layer.

'use strict';

const LINUX_PG_BIN_RE = /^\/usr\/lib\/postgresql\/\d+\/bin\/([a-zA-Z_]+)$/;
const LIBPQ_BIN = process.env.IMMICH_ACCELERATOR_LIBPQ_BIN
    || '/opt/homebrew/opt/libpq/bin';
const GNU_GZIP = process.env.IMMICH_ACCELERATOR_GNU_GZIP
    || '/opt/homebrew/bin/gzip';

// Cache the GNU-gzip existence check so we don't fstat on every
// spawn. Value is resolved lazily on first gzip rewrite.
let gnuGzipCached = null;
function hasGnuGzip() {
    if (gnuGzipCached !== null) return gnuGzipCached;
    try {
        require('fs').accessSync(GNU_GZIP, require('fs').constants.X_OK);
        gnuGzipCached = true;
    } catch (_e) {
        gnuGzipCached = false;
    }
    return gnuGzipCached;
}

function rewritePostgresBin(command) {
    if (typeof command !== 'string') return null;
    const m = command.match(LINUX_PG_BIN_RE);
    if (!m) return null;
    const rewritten = `${LIBPQ_BIN}/${m[1]}`;
    process.stderr.write(
        `[immich-accelerator] postgres client interpose: ${command} -> ${rewritten}\n`
    );
    return rewritten;
}

// Returns [command, args] with --rsyncable handled one of two ways:
//   (a) reroute to Homebrew GNU gzip (preserves rsync-friendly
//       block boundaries — upstream behavior)
//   (b) strip --rsyncable and let BSD gzip run (output is still a
//       valid gzip file, just without rsync optimization)
// Non-gzip calls pass through unmodified. Gzip calls without
// --rsyncable also pass through unmodified.
function rewriteGzip(command, args) {
    if (command !== 'gzip' || !Array.isArray(args)) {
        return [command, args];
    }
    const rsyncIdx = args.indexOf('--rsyncable');
    if (rsyncIdx === -1) {
        return [command, args];
    }
    if (hasGnuGzip()) {
        process.stderr.write(
            `[immich-accelerator] gzip interpose: routing to ${GNU_GZIP} for --rsyncable\n`
        );
        return [GNU_GZIP, args];
    }
    process.stderr.write(
        `[immich-accelerator] gzip interpose: stripping --rsyncable ` +
        `(GNU gzip not at ${GNU_GZIP}, BSD gzip fallback)\n`
    );
    const stripped = args.slice();
    stripped.splice(rsyncIdx, 1);
    return [command, stripped];
}

// Unified rewrite. Returns [command, args].
function rewriteSpawn(command, args) {
    const pgRewritten = rewritePostgresBin(command);
    if (pgRewritten !== null) {
        return [pgRewritten, args];
    }
    return rewriteGzip(command, args);
}

// Diagnostic (issue #89): a `spawn EBADF` crash in fluent-ffmpeg's capability
// detection reproduces on a reporter's machine but on no environment we can
// build here (matched Node version, shims, launchd stdio, concurrency, fd
// limits). Since `spawn` throws synchronously, we capture the file-descriptor
// state at the point of failure before re-throwing: which of fd 0/1/2 is
// invalid, the requested stdio, and the process's open fds. Fires only on the
// error path, so it is silent in normal operation and never alters behavior
// (the original error is always re-thrown).
function dumpSpawnFailure(command, options, err) {
    try {
        const fs = require('fs');
        const out = ['[immich-accelerator][spawn-debug] spawn threw: '
            + ((err && err.code) || (err && err.message) || err)];
        out.push('  command=' + command);
        out.push('  stdio=' + JSON.stringify(options && options.stdio));
        for (const fd of [0, 1, 2]) {
            try {
                fs.fstatSync(fd);
                out.push('  fd' + fd + '=ok');
            } catch (e) {
                out.push('  fd' + fd + '=BAD(' + ((e && e.code) || e) + ')');
            }
        }
        let open;
        try {
            open = fs.readdirSync('/dev/fd')
                .map(Number).filter(function (n) { return !Number.isNaN(n); })
                .sort(function (x, y) { return x - y; });
        } catch (e) {
            open = '/dev/fd unreadable: ' + ((e && e.code) || e);
        }
        out.push('  open_fds=' + JSON.stringify(open));
        process.stderr.write(out.join('\n') + '\n');
    } catch (_e) {
        // diagnostics must never mask or replace the original failure
    }
}

// Node caches modules by specifier, so `require('child_process')` and
// `require('node:child_process')` may or may not be the same object
// depending on Node version. Patch both to be safe.
function install(moduleSpecifier) {
    let cp;
    try {
        cp = require(moduleSpecifier);
    } catch (_err) {
        return;  // specifier not supported on this Node version
    }

    // `require('child_process')` and `require('node:child_process')` return the
    // SAME object on modern Node, so calling install() for both specifiers would
    // wrap spawn twice: the second wrap captures the first wrapper as its
    // "original", and every spawn then runs through two nested wrappers. Harmless
    // (each forwards the same args) but it doubles the work and shows up as two
    // confusing pg_dump_shim frames in stack traces. Guard so we patch a given
    // module object once. (Issue #89.)
    if (cp.__immichAccelPatched) return;
    cp.__immichAccelPatched = true;

    const origSpawn = cp.spawn;
    cp.spawn = function (command, args, options) {
        const [c, a] = rewriteSpawn(command, args);
        try {
            return origSpawn.call(this, c, a, options);
        } catch (err) {
            dumpSpawnFailure(c, options, err);
            throw err;
        }
    };

    const origSpawnSync = cp.spawnSync;
    cp.spawnSync = function (command, args, options) {
        const [c, a] = rewriteSpawn(command, args);
        return origSpawnSync.call(this, c, a, options);
    };

    const origExecFile = cp.execFile;
    if (typeof origExecFile === 'function') {
        cp.execFile = function (file, args, ...rest) {
            // execFile's signature is (file, args?, options?, cb?).
            // If args is an array, rewrite both; if it's options or
            // callback, leave everything but the file path alone.
            if (Array.isArray(args)) {
                const [c, a] = rewriteSpawn(file, args);
                return origExecFile.call(this, c, a, ...rest);
            }
            const pgRewritten = rewritePostgresBin(file);
            return origExecFile.call(
                this, pgRewritten !== null ? pgRewritten : file, args, ...rest
            );
        };
    }
}

install('child_process');
install('node:child_process');
