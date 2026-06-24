// pg_keepalive_shim.js
//
// Runtime interposition to enable TCP keepalive on Immich's Postgres
// connections (issue #74).
//
// In a split deployment the worker holds long-lived connections across the
// network to a remote Postgres. A stateful firewall or NAT between them (e.g.
// the worker on one VLAN, the database on another) silently reaps a connection
// once it has been idle past the firewall's timeout. node-postgres never learns
// the socket is gone, so the next query's read just hangs until it fails with
// `read ETIMEDOUT`, and the worker does not recover. This is intermittent by
// nature — only idle connections get reaped, active ones survive — which is
// what makes it confusing to diagnose.
//
// node-postgres supports `keepAlive` on the socket, but Immich doesn't expose
// it as an env var (it's a Pool/Client option, not part of DB_URL). Rather than
// patch Immich's source (which would break the "unmodified" invariant), we
// preload this module via `NODE_OPTIONS=--require ...` and wrap the `pg`
// module's Pool and Client constructors so every connection sets keepAlive,
// keeping the socket warm so the firewall never considers it idle.
//
// Same interposition pattern as the pg_dump and HEIC shims. Off the hot path
// (only runs at connection construction) and a no-op for same-host setups where
// nothing reaps the connection. Opt-out / tune via env (see below).

'use strict';

// keepAlive is benign on a healthy LAN, so default it on. The initial delay is
// how long a connection sits idle before the first keepalive probe; 10s is well
// under typical firewall idle timeouts (often 60–300s) while staying cheap.
const ENABLED = process.env.IMMICH_ACCEL_PG_KEEPALIVE !== '0';
const DELAY_MS = parseInt(
    process.env.IMMICH_ACCEL_PG_KEEPALIVE_MS || '10000', 10
);

function withKeepAlive(config) {
    // pg accepts the config as the first constructor arg (object or undefined;
    // a connection-string shorthand is also allowed). Only augment the object
    // form, and never override an explicit choice the caller already made.
    if (config == null || typeof config !== 'object') {
        config = { connectionString: config == null ? undefined : config };
    }
    if (config.keepAlive === undefined) {
        config.keepAlive = true;
    }
    if (config.keepAliveInitialDelayMillis === undefined) {
        config.keepAliveInitialDelayMillis = DELAY_MS;
    }
    return config;
}

// A construct-trap Proxy preserves instanceof, the prototype chain, and static
// properties (kysely and pg's own internals reference these), unlike wrapping
// the class in a plain function.
function wrapCtor(Original) {
    return new Proxy(Original, {
        construct(target, args, newTarget) {
            const next = args.slice();
            next[0] = withKeepAlive(next[0]);
            return Reflect.construct(target, next, newTarget);
        },
    });
}

function patchPg(pg) {
    if (pg.Pool) pg.Pool = wrapCtor(pg.Pool);
    if (pg.Client) pg.Client = wrapCtor(pg.Client);
    process.stderr.write(
        `[immich-accelerator] pg keepalive enabled (initial delay ${DELAY_MS}ms)\n`
    );
}

if (ENABLED) {
    const Module = require('module');
    const origLoad = Module._load;
    Module._load = function (request, parent, isMain) {
        const mod = origLoad.apply(this, arguments);
        if (request === 'pg' && mod && !mod.__keepAlivePatched) {
            try {
                patchPg(mod);
                mod.__keepAlivePatched = true;
            } catch (e) {
                process.stderr.write(
                    '[immich-accelerator] pg keepalive shim failed: ' +
                    String((e && e.message) || e) + '\n'
                );
            }
        }
        return mod;
    };
}
