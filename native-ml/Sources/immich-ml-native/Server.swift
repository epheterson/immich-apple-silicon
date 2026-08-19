import Foundation
import Network

// HTTP server answering Immich's ML contract (/, /ping, /health, /predict) with
// Network.framework — replaces FastAPI/uvicorn. Reads Content-Length bodies,
// one request per connection. A concurrency cap mirrors the Python service's
// max_concurrent_requests backpressure.
// Ceilings for one connection. This server answers Immich — the worker on this
// Mac, and the API server for search text embeddings — but the transport
// enforced none of that, and every unbounded path below was reachable by
// anything able to open a TCP connection to the port.
//
// 64 KiB of headers is orders of magnitude above Immich's own requests and far
// below what threatens an 8 GB Mac. 64 MiB of body covers the largest preview
// Immich sends. The header deadline ends a peer that opens a connection and
// then says nothing; the connection deadline is generous enough for a first-use
// model fetch, which legitimately holds a /predict open for minutes.
private let maxHeaderBytes = 64 * 1024
private let maxBodyBytes = 64 * 1024 * 1024
private let headerDeadlineSeconds: TimeInterval = 10
private let connectionDeadlineSeconds: TimeInterval = 300

// A connection, and the one place that ends one.
//
// Every terminal path used to be a bare `return`, or a `cancel()` buried in a
// send completion. The path taken when the headers never completed and the peer
// had already gone did neither, so the NWConnection stayed alive holding its
// descriptor. Measured against this server: inside 1,000 aborted requests it
// stopped accepting connections at 349 open descriptors, still holding the
// listening socket. macOS gives the process a 256-descriptor soft limit, and
// processes serving a library here have been found in the same state at 350-353
// descriptors, most of them CLOSED — which to anything supervising it looks
// like a service that is up and answering nothing.
//
// What follows bounds that; it does not zero it. 3,000 aborted requests still
// leave 11-29 sockets in CLOSED state, from a cause I have not found. Normal
// requests leave none, and connections ended by the deadline below leave none.
private final class Conn {
    let nw: NWConnection
    private let lock = NSLock()
    private var done = false
    private var timer: DispatchSourceTimer?
    private let opened = DispatchTime.now()

    init(_ nw: NWConnection) { self.nw = nw }

    // Armed from the moment the connection is accepted and re-armed rather than
    // cleared once the request is in hand: a peer that completes its headers and
    // then stalls must not hold a descriptor any longer than one that never
    // sends them at all.
    func arm(_ seconds: TimeInterval) {
        let t = DispatchSource.makeTimerSource(queue: .global())
        t.schedule(deadline: .now() + seconds)
        t.setEventHandler { [weak self] in self?.finish() }
        // Active before anything else can reach it. finish() can land between
        // here and the store below — a peer that resets the connection the
        // moment it is accepted does exactly that — and releasing a dispatch
        // source that was never activated traps the process, which is the same
        // way of dying this commit exists to remove.
        t.activate()
        lock.lock()
        let already = done
        if !already {
            timer?.cancel()
            timer = t
        }
        lock.unlock()
        // Finished while this one was being built: end it, and leave nothing
        // installed on a connection that is already over.
        if already { t.cancel() }
    }

    // Idempotent. Whichever of EOF, protocol error, size cap, deadline or a
    // completed response arrives first is the one that ends the connection; the
    // rest are no-ops.
    func finish() {
        lock.lock()
        let already = done
        done = true
        let t = timer
        timer = nil
        lock.unlock()
        guard !already else { return }
        t?.cancel()
        nw.cancel()
    }

    // What is left of the connection's total budget, so that time already spent
    // reading the request is not handed back for inference. Measured on the
    // same clock the timers run on: a wall-clock reading would move when the
    // system clock is corrected.
    var remainingBudget: TimeInterval {
        let spent = Double(DispatchTime.now().uptimeNanoseconds - opened.uptimeNanoseconds) / 1e9
        return max(0, connectionDeadlineSeconds - spent)
    }
}

func startServer(port: UInt16, models: Models, maxConcurrent: Int = 4) {
    let sem = DispatchSemaphore(value: maxConcurrent)
    let listener = try! NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: port)!)
    // NWListener reports a bind failure asynchronously through this handler, it
    // does not throw from the initializer. Without a handler the failure is
    // swallowed whole: the process stays parked in dispatchMain() forever,
    // listening to nothing, looking alive to anything that only checks whether
    // the PID exists. Every subsequent start then leaks another one, and
    // ml.pid ends up naming a process that serves no traffic while some older
    // instance still owns the port, so `restart` politely kills the wrong
    // process and changes nothing. Measured on a real box: 6 leaked instances
    // over 4 days, `status` reporting a PID that was not the listener.
    // Exiting instead makes the failure visible and lets the supervisor act.
    listener.stateUpdateHandler = { state in
        switch state {
        case .ready:
            print("[native-ml] listening on port \(port)")
        case .failed(let error):
            FileHandle.standardError.write(
                Data("[native-ml] cannot listen on port \(port): \(error)\n".utf8))
            exit(70)   // EX_SOFTWARE
        case .waiting(let error):
            // Transient by contract (Network.framework retries on its own), so
            // not fatal. Logged because a listener stuck here answers nothing
            // and the silence is otherwise indistinguishable from idle.
            FileHandle.standardError.write(
                Data("[native-ml] waiting to listen on port \(port): \(error)\n".utf8))
        default:
            break
        }
    }
    listener.newConnectionHandler = { nwConn in
        let conn = Conn(nwConn)
        // A connection that dies on its own still has to release the timer and
        // mark itself finished, or the deadline fires against a dead peer.
        nwConn.stateUpdateHandler = { state in
            switch state {
            case .failed, .cancelled: conn.finish()
            default: break
            }
        }
        nwConn.start(queue: .global())
        conn.arm(headerDeadlineSeconds)
        readRequest(conn, buffer: Data(), models: models, sem: sem)
    }
    listener.start(queue: .global())
}

private func readRequest(_ conn: Conn, buffer: Data, models: Models, sem: DispatchSemaphore) {
    conn.nw.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { chunk, _, done, err in
        var buf = buffer
        if let chunk = chunk { buf.append(chunk) }
        guard let hdrEnd = buf.range(of: Data("\r\n\r\n".utf8)) else {
            // Cap what an unterminated header block may cost before concluding
            // it is not a request at all. The receive maximum below is per
            // callback, so without this the buffer grew without limit.
            if buf.count > maxHeaderBytes {
                respond(conn, status: "431 Request Header Fields Too Large",
                        body: Data(), ctype: "text/plain")
                return
            }
            if err == nil && !done {
                readRequest(conn, buffer: buf, models: models, sem: sem)
            } else {
                // The peer stopped mid-headers. This is where the descriptor
                // leak was: the old code returned here and left the connection
                // alive forever.
                conn.finish()
            }
            return
        }
        // The cap above only sees a header block that has not ended yet. Without
        // this one, a terminator arriving in the same callback lets a block
        // through at the receive maximum instead of at the limit.
        if hdrEnd.lowerBound > maxHeaderBytes {
            respond(conn, status: "431 Request Header Fields Too Large",
                    body: Data(), ctype: "text/plain")
            return
        }
        let head = String(data: buf[..<hdrEnd.lowerBound], encoding: .utf8) ?? ""
        let lines = head.components(separatedBy: "\r\n")
        let reqLine = lines.first ?? ""
        let parts = reqLine.components(separatedBy: " ")
        let method = parts.first ?? "", path = parts.count > 1 ? parts[1] : "/"

        // Content-Length, without trusting the header to be well formed. The
        // previous form indexed `split(separator: ":")[1]`, and split drops
        // empty subsequences: a bare `Content-Length:` yields one element, so
        // indexing past it aborted the process. One such request, from anywhere
        // that could reach the port, killed the service.
        var clen = 0
        if let lengthLine = lines.first(where: { $0.lowercased().hasPrefix("content-length:") }) {
            let raw = lengthLine.dropFirst("content-length:".count)
                .trimmingCharacters(in: .whitespaces)
            guard let declared = Int(raw), declared >= 0 else {
                respond(conn, status: "400 Bad Request", body: Data(), ctype: "text/plain")
                return
            }
            clen = declared
        }
        if clen > maxBodyBytes {
            respond(conn, status: "413 Content Too Large", body: Data(), ctype: "text/plain")
            return
        }
        // Headers are complete and well formed, so the tight header deadline has
        // done its job. Hand the rest of the connection its own budget now, not
        // after the body arrives: a legitimate multi-megabyte /predict upload
        // must not be judged against the deadline meant for a peer that never
        // finished its request line.
        conn.arm(conn.remainingBudget)
        let ctype = lines.first { $0.lowercased().hasPrefix("content-type:") } ?? ""
        let body = buf[hdrEnd.upperBound...]
        // Bound what arrives as well as what was declared: a declared length
        // under the ceiling can still be overshot within one callback.
        if body.count > maxBodyBytes {
            respond(conn, status: "413 Content Too Large", body: Data(), ctype: "text/plain")
            return
        }
        if body.count < clen {
            if err == nil && !done {
                readRequest(conn, buffer: buf, models: models, sem: sem)
            } else {
                // The peer stopped before delivering what it declared. This used
                // to fall through and be processed as though it were complete.
                respond(conn, status: "400 Bad Request", body: Data(), ctype: "text/plain")
            }
            return
        }
        // Only /predict is gated by the concurrency cap. /ping and /health must
        // answer even while all slots are blocked on a first-use model download,
        // or the watchdog/menubar would see a healthy service as down and a
        // restart would kill the download mid-transfer.
        if path == "/predict" {
            sem.wait()
            defer { sem.signal() }
            handle(conn, method: method, path: path, ctype: ctype, body: Data(body), models: models)
        } else {
            handle(conn, method: method, path: path, ctype: ctype, body: Data(body), models: models)
        }
    }
}

private func respond(_ conn: Conn, status: String, body: Data, ctype: String = "application/json") {
    let head = "HTTP/1.1 \(status)\r\nContent-Type: \(ctype)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
    var out = Data(head.utf8); out.append(body)
    conn.nw.send(content: out, completion: .contentProcessed { _ in conn.finish() })
}

private func respondJSON(_ conn: Conn, status: String, object: Any) {
    let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
    respond(conn, status: status, body: data)
}

private func handle(_ conn: Conn, method: String, path: String, ctype: String, body: Data, models: Models) {
    switch path {
    case "/":
        respondJSON(conn, status: "200 OK", object: ["message": "Immich ML"])
    case "/ping":
        respond(conn, status: "200 OK", body: Data("pong".utf8), ctype: "text/plain")
    case "/health":
        let face = models.arcfaceAvailable ? "ok" : "error: model not found"
        var health: [String: Any] = [
            "status": models.arcfaceAvailable ? "healthy" : "degraded",
            "stub_mode": false,
            "checks": ["clip": "ok", "face_recognition": face, "vision_framework": "ok"],
        ]
        // A model fetch can run for minutes on the largest models, during which
        // Immich's jobs time out and retry. Report it so the menu bar can say
        // what is happening instead of just looking broken.
        if let p = ZooCLIP.fetchProgress {
            health["downloading"] = [
                "model": p.model, "files_done": p.done, "files_total": p.total,
            ]
        }
        respondJSON(conn, status: "200 OK", object: health)
    case "/predict":
        // Mirrors ml/src/main.py's request-logging middleware + _process_predict
        // line-for-line (see Predict.swift for the rest): same message content,
        // just prefixed with [native-ml] instead of the python logger's
        // "TIMESTAMP - src.main - INFO -" preamble. Skips /ping and /health,
        // which are polled every few seconds and would just be noise.
        print("[native-ml] \(method) /predict")
        guard let boundary = ctype.range(of: "boundary=").map({
            String(ctype[$0.upperBound...]).trimmingCharacters(in: .whitespaces)
        }) else {
            respondJSON(conn, status: "400 Bad Request", object: ["detail": "expected multipart/form-data"]); return
        }
        guard let entriesData = extractPart(body, boundary: boundary, name: "entries"),
              let entriesStr = String(data: entriesData, encoding: .utf8) else {
            respondJSON(conn, status: "422 Unprocessable Entity", object: ["detail": "missing entries field"]); return
        }
        guard let entries = (try? JSONSerialization.jsonObject(with: Data(entriesStr.utf8))) as? [String: Any] else {
            // The raw field is logged only when it failed to parse. Logging it
            // on every request put a copy of the entries JSON in ml.log for
            // each of a library's assets, which is noise on a six-figure
            // import and tells you nothing the per-task lines don't.
            print("[native-ml] Invalid entries JSON: \(entriesStr)")
            respondJSON(conn, status: "422 Unprocessable Entity", object: ["detail": "invalid entries JSON"]); return
        }
        let image = extractPart(body, boundary: boundary, name: "image")
        let text = extractPart(body, boundary: boundary, name: "text").flatMap { String(data: $0, encoding: .utf8) }
        do {
            let response = try processPredict(entries: entries, imageData: image, text: text, models: models)
            respondJSON(conn, status: "200 OK", object: response)
        } catch let e as PredictError {
            respondJSON(conn, status: e.status, object: ["detail": e.message])
        } catch {
            respondJSON(conn, status: "500 Internal Server Error", object: ["detail": "internal error"])
        }
    default:
        respondJSON(conn, status: "404 Not Found", object: [:] as [String: Any])
    }
}

// Extract the bytes of a multipart part named `name` (file or form field).
private func extractPart(_ body: Data, boundary: String, name: String) -> Data? {
    let delim = Data("--\(boundary)".utf8)
    var idx = body.startIndex
    var ranges: [Range<Data.Index>] = []
    while let r = body.range(of: delim, in: idx..<body.endIndex) {
        ranges.append(r); idx = r.upperBound
    }
    guard ranges.count >= 2 else { return nil }
    for i in 0..<(ranges.count - 1) {
        let part = body[ranges[i].upperBound..<ranges[i + 1].lowerBound]
        guard let hEnd = part.range(of: Data("\r\n\r\n".utf8)) else { continue }
        let hdr = String(data: part[..<hEnd.lowerBound], encoding: .utf8) ?? ""
        if hdr.contains("name=\"\(name)\"") {
            var content = part[hEnd.upperBound...]
            if content.count >= 2 { content = content.dropLast(2) }   // trailing \r\n
            return Data(content)
        }
    }
    return nil
}
