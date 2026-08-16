// job_retry_shim.js
//
// Runtime interposition to give Immich's BullMQ queues automatic retries
// that never permanently fail, with exponential backoff that resets to its
// short initial delay whenever the accelerator restarts.
//
// Immich hardcodes `defaultJobOptions: { attempts: 1, ... }` for every queue
// (config.repository.js), so any single failure — a dropped connection to a
// remote Postgres/Redis in a split deployment, a transient SMB hiccup on the
// library mount — permanently fails the job with zero retry. There's no env
// var for this; it's baked into the object Immich builds and hands to
// `BullModule.forRoot()`. Rather than patch Immich's source (which would
// break the "unmodified" invariant), we preload this module via
// `NODE_OPTIONS=--require ...` and interpose on two points in `bullmq`.
//
// Neither `Queue` nor `Worker` can be wrapped at the constructor/export
// level: bullmq's compiled CJS index re-exports both via tslib's
// __exportStar, which defines them as getters with `configurable: false`
// (ESM live-binding emulation) — empirically verified against the real
// installed package, both plain assignment and Object.defineProperty throw.
// So instead:
//
//   1. `Queue.prototype.add`/`addBulk` (ordinary writable, configurable
//      methods) are wrapped to inject a very high `attempts` ceiling and a
//      custom backoff marker into every job's options, unless the caller
//      already asked for something explicit.
//
//   2. The actual retry delay is decided worker-side, not queue-side:
//      `Job.shouldRetryJob()` reads `this.queue.opts.settings.backoffStrategy`,
//      and for a job being processed `this.queue` is the *Worker* instance
//      (`Worker extends QueueBase`; `Job.fromJSON(this, ...)` is called with
//      `this` bound to the Worker) — not the producer-side Queue object, and
//      `@nestjs/bullmq` builds each Worker its own fresh options object that
//      never inherits the Queue's `defaultJobOptions`/`settings`. So we wrap
//      `Worker.prototype.run` (called once per worker, before it processes
//      anything) to inject our backoff strategy into `this.opts.settings`.
//
// The backoff strategy itself tracks attempt counts in a process-local Map
// keyed by `queueName:jobId`, deliberately ignoring bullmq's own
// Redis-persisted `attemptsMade` (which never resets). A restart clears the
// Map along with the rest of the process's memory, so any job that was deep
// into a long backoff before the restart starts ramping from the short
// initial delay again — appropriate here, since a restart usually means
// whatever connection was flaky got a fresh start too. Delay is capped so a
// job that keeps failing in a single long-running process still gets
// retried at a bounded interval instead of the uncapped 2^n growth of
// bullmq's builtin `exponential` strategy.
//
// A job that keeps failing for a real reason (corrupt file, permanently
// missing asset) will keep retrying forever rather than ending up
// permanently "failed" — that's the intended behavior for this shim, at the
// cost of never getting a clean signal to stop trying. Off the hot path
// relative to actual image processing. Opt-out / tune via env (see below).

'use strict';

const ENABLED = process.env.IMMICH_ACCEL_JOB_RETRY !== '0';
// Effectively unlimited by default — bullmq requires a finite number, and
// this is high enough that no real job will ever exhaust it.
const ATTEMPTS = parseInt(
    process.env.IMMICH_ACCEL_JOB_RETRY_ATTEMPTS || String(Number.MAX_SAFE_INTEGER), 10
);
const BASE_MS = parseInt(
    process.env.IMMICH_ACCEL_JOB_RETRY_BACKOFF_MS || '10000', 10
);
const MAX_MS = parseInt(
    process.env.IMMICH_ACCEL_JOB_RETRY_BACKOFF_MAX_MS || String(5 * 60 * 1000), 10
);
const BACKOFF_TYPE = 'immich-accel';

// Process-local attempt counter, keyed by `queueName:jobId`. Not bullmq's
// `attemptsMade` (Redis-persisted, never resets) — deliberately reset by a
// process restart. Bounded so a long-running process can't leak memory
// tracking every job id it has ever seen; clearing it early just means a
// handful of jobs mid-backoff restart their ramp, which is harmless.
const localAttempts = new Map();
const MAX_TRACKED = 50000;

function backoffStrategy(_attemptsMade, _type, _err, job) {
    if (localAttempts.size > MAX_TRACKED) {
        localAttempts.clear();
    }
    const queueName = (job && job.queueName) || (job && job.queue && job.queue.name) || '?';
    const key = `${queueName}:${job && job.id}`;
    const n = (localAttempts.get(key) || 0) + 1;
    localAttempts.set(key, n);
    return Math.min(BASE_MS * Math.pow(2, n - 1), MAX_MS);
}

function withRetry(opts) {
    // add()/addBulk() accept opts as an object (or undefined/null). Only
    // augment the object form, and never override an explicit choice the
    // caller already made — Immich's own value here is what we intend to
    // override, but a future Immich version setting something other than
    // the current hardcoded default (or a caller in a test harness) should
    // win.
    const next = opts && typeof opts === 'object' ? { ...opts } : {};
    if (next.attempts === undefined || next.attempts === 1) {
        next.attempts = ATTEMPTS;
    }
    if (next.backoff === undefined) {
        next.backoff = { type: BACKOFF_TYPE };
    }
    return next;
}

function patchQueuePrototype(QueueProto) {
    const origAdd = QueueProto.add;
    QueueProto.add = function (name, data, opts) {
        return origAdd.call(this, name, data, withRetry(opts));
    };

    const origAddBulk = QueueProto.addBulk;
    QueueProto.addBulk = function (jobs) {
        const next = jobs.map((job) => ({ ...job, opts: withRetry(job.opts) }));
        return origAddBulk.call(this, next);
    };
}

function patchWorkerPrototype(WorkerProto) {
    const origRun = WorkerProto.run;
    WorkerProto.run = function (...args) {
        if (!this.opts) this.opts = {};
        if (!this.opts.settings) this.opts.settings = {};
        if (!this.opts.settings.backoffStrategy) {
            this.opts.settings.backoffStrategy = backoffStrategy;
        }
        return origRun.apply(this, args);
    };
}

function patchBullmq(bullmq) {
    if (bullmq.Queue && bullmq.Queue.prototype && bullmq.Queue.prototype.add) {
        patchQueuePrototype(bullmq.Queue.prototype);
    }
    if (bullmq.Worker && bullmq.Worker.prototype && bullmq.Worker.prototype.run) {
        patchWorkerPrototype(bullmq.Worker.prototype);
    }
    process.stderr.write(
        `[immich-accelerator] job retry enabled (attempts effectively unlimited, ` +
        `backoff ${BASE_MS}ms exponential capped at ${MAX_MS}ms, resets on restart)\n`
    );
}

if (ENABLED) {
    const Module = require('module');
    const origLoad = Module._load;
    Module._load = function (request, parent, isMain) {
        const mod = origLoad.apply(this, arguments);
        if (request === 'bullmq' && mod && !mod.__jobRetryPatched) {
            try {
                patchBullmq(mod);
                mod.__jobRetryPatched = true;
            } catch (e) {
                process.stderr.write(
                    '[immich-accelerator] job retry shim failed: ' +
                    String((e && e.message) || e) + '\n'
                );
            }
        }
        return mod;
    };
}
