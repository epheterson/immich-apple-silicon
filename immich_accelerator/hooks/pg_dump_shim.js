// pg_dump_shim.js
//
// Runtime interposition for Immich's DatabaseBackupService. Immich
// hardcodes `/usr/lib/postgresql/${version}/bin/pg_dump` (and
// pg_dumpall, pg_restore, psql) in dist/services/database-backup.
// service.js — a Linux-only path that doesn't exist on macOS. On
// a native macOS microservices worker, every backup cycle fails
// with ENOENT (issue #24). There's no env-var escape hatch in the
// upstream code, and `/usr/lib` is SIP-protected so we can't put
// a file there ourselves.
//
// Instead: preload this module via `NODE_OPTIONS=--require ...`
// before the Immich worker starts. We monkey-patch child_process's
// spawn / spawnSync / execFile to rewrite the Linux postgres client
// path to the Homebrew libpq path. Immich's source files on disk
// are NEVER touched — the README's "unmodified" claim stays true.
//
// This is the same interposition pattern the accelerator already
// uses for the ffmpeg wrapper (PATH-based), just at the Node
// module layer because Immich uses an absolute path for pg_dump.

'use strict';

const LINUX_PG_BIN_RE = /^\/usr\/lib\/postgresql\/\d+\/bin\/([a-zA-Z_]+)$/;
const LIBPQ_BIN = process.env.IMMICH_ACCELERATOR_LIBPQ_BIN
    || '/opt/homebrew/opt/libpq/bin';

function rewrite(command) {
    if (typeof command !== 'string') return command;
    const m = command.match(LINUX_PG_BIN_RE);
    if (!m) return command;
    const rewritten = `${LIBPQ_BIN}/${m[1]}`;
    process.stderr.write(
        `[immich-accelerator] postgres client interpose: ${command} -> ${rewritten}\n`
    );
    return rewritten;
}

// Node caches modules by specifier, so `require('child_process')` and
// `require('node:child_process')` may or may not be the same object
// depending on Node version. Patch both to be safe.
function install(moduleSpecifier) {
    let cp;
    try {
        cp = require(moduleSpecifier);
    } catch (err) {
        return;  // specifier not supported on this Node version
    }

    const origSpawn = cp.spawn;
    cp.spawn = function (command, args, options) {
        return origSpawn.call(this, rewrite(command), args, options);
    };

    const origSpawnSync = cp.spawnSync;
    cp.spawnSync = function (command, args, options) {
        return origSpawnSync.call(this, rewrite(command), args, options);
    };

    const origExecFile = cp.execFile;
    if (typeof origExecFile === 'function') {
        cp.execFile = function (file, ...rest) {
            return origExecFile.call(this, rewrite(file), ...rest);
        };
    }
}

install('child_process');
install('node:child_process');
