import Foundation
import Network

// HTTP server answering Immich's ML contract (/, /ping, /health, /predict) with
// Network.framework — replaces FastAPI/uvicorn. Reads Content-Length bodies,
// one request per connection. A concurrency cap mirrors the Python service's
// max_concurrent_requests backpressure.
func startServer(port: UInt16, models: Models, maxConcurrent: Int = 4) {
    let sem = DispatchSemaphore(value: maxConcurrent)
    let listener = try! NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: port)!)
    listener.newConnectionHandler = { conn in
        conn.start(queue: .global())
        readRequest(conn, buffer: Data(), models: models, sem: sem)
    }
    listener.start(queue: .global())
}

private func readRequest(_ conn: NWConnection, buffer: Data, models: Models, sem: DispatchSemaphore) {
    conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { chunk, _, done, err in
        var buf = buffer
        if let chunk = chunk { buf.append(chunk) }
        guard let hdrEnd = buf.range(of: Data("\r\n\r\n".utf8)) else {
            if err == nil && !done { readRequest(conn, buffer: buf, models: models, sem: sem) }
            return
        }
        let head = String(data: buf[..<hdrEnd.lowerBound], encoding: .utf8) ?? ""
        let lines = head.components(separatedBy: "\r\n")
        let reqLine = lines.first ?? ""
        let parts = reqLine.components(separatedBy: " ")
        let method = parts.first ?? "", path = parts.count > 1 ? parts[1] : "/"
        let clen = lines.first { $0.lowercased().hasPrefix("content-length:") }
            .flatMap { Int($0.split(separator: ":")[1].trimmingCharacters(in: .whitespaces)) } ?? 0
        let ctype = lines.first { $0.lowercased().hasPrefix("content-type:") } ?? ""
        let body = buf[hdrEnd.upperBound...]
        if body.count < clen && err == nil && !done {
            readRequest(conn, buffer: buf, models: models, sem: sem); return
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

private func respond(_ conn: NWConnection, status: String, body: Data, ctype: String = "application/json") {
    let head = "HTTP/1.1 \(status)\r\nContent-Type: \(ctype)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
    var out = Data(head.utf8); out.append(body)
    conn.send(content: out, completion: .contentProcessed { _ in conn.cancel() })
}

private func respondJSON(_ conn: NWConnection, status: String, object: Any) {
    let data = (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
    respond(conn, status: status, body: data)
}

private func handle(_ conn: NWConnection, method: String, path: String, ctype: String, body: Data, models: Models) {
    switch path {
    case "/":
        respondJSON(conn, status: "200 OK", object: ["message": "Immich ML"])
    case "/ping":
        respond(conn, status: "200 OK", body: Data("pong".utf8), ctype: "text/plain")
    case "/health":
        let face = models.arcface != nil ? "ok" : "error: model not loaded"
        respondJSON(conn, status: "200 OK", object: [
            "status": models.arcface != nil ? "healthy" : "degraded",
            "stub_mode": false,
            "checks": ["clip": "ok", "face_recognition": face, "vision_framework": "ok"],
        ])
    case "/predict":
        guard let boundary = ctype.range(of: "boundary=").map({
            String(ctype[$0.upperBound...]).trimmingCharacters(in: .whitespaces)
        }) else {
            respondJSON(conn, status: "400 Bad Request", object: ["detail": "expected multipart/form-data"]); return
        }
        guard let entriesData = extractPart(body, boundary: boundary, name: "entries"),
              let entriesStr = String(data: entriesData, encoding: .utf8),
              let entries = (try? JSONSerialization.jsonObject(with: Data(entriesStr.utf8))) as? [String: Any] else {
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
