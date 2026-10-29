// The program the Homebrew service runs: it starts `immich-accelerator watch`
// and stays its parent for as long as it runs.
//
// macOS asks about network volumes and the local network on behalf of the
// process launchd started, and everything below it inherits that answer. When
// that process was Homebrew's Python, ad-hoc signed and replaced on every
// `brew upgrade`, each upgrade was a stranger to macOS: a new prompt on a
// screen nobody was watching, and every NAS read in the service blocked on it.
// This bundle is signed with the project's own certificate under a fixed
// identifier, so the Allow a person gives it once survives our updates,
// Python's and Node's.
//
// It takes no arguments and only ever runs the accelerator next to it, so a
// signed program holding that permission is not a way to run anything else
// with it. It must stay the parent (spawn, never exec): exec would make
// Python the process macOS asks about again.
import Foundation

let keg = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
let cli = keg.appendingPathComponent("bin/immich-accelerator").path
let argv: [UnsafeMutablePointer<CChar>?] = [strdup(cli), strdup("watch"), nil]

// Lets the watcher see that it runs under this, and so on a stable identity.
setenv("IMMICH_ACCELERATOR_LAUNCHER", Bundle.main.bundlePath, 1)

// Watch for launchd's stop before starting anything, so a SIGTERM that lands
// while the watcher is being spawned is held and passed on, not lost with the
// launcher's default exit. The handlers run from dispatchMain, by which time
// pid is set.
let forwarded: [Int32] = [SIGTERM, SIGINT, SIGHUP]
var pid: pid_t = 0
var sources: [DispatchSourceSignal] = []
for sig in forwarded {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig)
    source.setEventHandler { if pid > 0 { kill(pid, sig) } }
    source.resume()
    sources.append(source)
}

// The watcher gets those signals back at their defaults; ignoring them is
// only how this process hands them to the dispatch sources.
var attr: posix_spawnattr_t? = nil
posix_spawnattr_init(&attr)
var defaults = sigset_t()
sigemptyset(&defaults)
for sig in forwarded { sigaddset(&defaults, sig) }
posix_spawnattr_setsigdefault(&attr, &defaults)
posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETSIGDEF))

let rc = posix_spawn(&pid, cli, nil, &attr, argv, environ)
posix_spawnattr_destroy(&attr)
guard rc == 0 else {
    fputs("immich-accelerator-service: cannot start \(cli): \(String(cString: strerror(rc)))\n", stderr)
    exit(127)
}

// Exit the way the watcher did, so launchd's KeepAlive sees what it always saw.
Thread.detachNewThread {
    var status: Int32 = 0
    while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
    let sig = status & 0x7f
    exit(sig == 0 ? (status >> 8) & 0xff : 128 + sig)
}
dispatchMain()
