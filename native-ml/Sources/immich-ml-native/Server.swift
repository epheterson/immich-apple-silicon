import Foundation
import Network

// Minimal HTTP server answering immich-ml-metal's contract (/ping, /health,
// /predict) with Network.framework — replaces FastAPI/uvicorn. Prototype-grade
// (single-shot request per connection, Content-Length bodies), enough to prove a
// native binary can be a drop-in for the Python ML service on the CLIP path.

func startServer(port: UInt16, clip: CLIPEncoder) {
    let listener = try! NWListener(using: .tcp, on: NWEndpoint.Port(rawValue: port)!)
    listener.newConnectionHandler = { conn in
        conn.start(queue: .global())
        readRequest(conn, buffer: Data(), clip: clip)
    }
    listener.start(queue: .global())
}

private func readRequest(_ conn: NWConnection, buffer: Data, clip: CLIPEncoder) {
    conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { chunk, _, done, err in
        var buf = buffer
        if let chunk = chunk { buf.append(chunk) }
        // wait for full headers
        guard let hdrEnd = buf.range(of: Data("\r\n\r\n".utf8)) else {
            if err == nil && !done { readRequest(conn, buffer: buf, clip: clip) }
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
            readRequest(conn, buffer: buf, clip: clip); return
        }
        handle(conn, method: method, path: path, ctype: ctype, body: Data(body), clip: clip)
    }
}

private func respond(_ conn: NWConnection, status: String, body: Data, ctype: String = "application/json") {
    var head = "HTTP/1.1 \(status)\r\nContent-Type: \(ctype)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
    var out = Data(head.utf8); out.append(body)
    conn.send(content: out, completion: .contentProcessed { _ in conn.cancel() })
    _ = head
}

private func handle(_ conn: NWConnection, method: String, path: String, ctype: String, body: Data, clip: CLIPEncoder) {
    switch path {
    case "/ping":
        respond(conn, status: "200 OK", body: Data("pong".utf8), ctype: "text/plain")
    case "/health":
        respond(conn, status: "200 OK", body: Data("{\"status\":\"healthy\",\"checks\":[\"clip\"],\"stub_mode\":false}".utf8))
    case "/predict":
        guard let boundary = ctype.range(of: "boundary=").map({ String(ctype[$0.upperBound...]).trimmingCharacters(in: .whitespaces) }),
              let img = extractPart(body, boundary: boundary, name: "image"),
              let cg = loadCGImage(data: img) else {
            respond(conn, status: "400 Bad Request", body: Data("{\"error\":\"no image\"}".utf8)); return
        }
        let emb = clip.embed(cg)
        // Immich wire format: clip embedding as a Python-list-repr STRING (main.py str(tolist()))
        let listStr = "[" + emb.map { String($0) }.joined(separator: ", ") + "]"
        let json = "{\"clip\": \(escapeJSON(listStr))}"
        respond(conn, status: "200 OK", body: Data(json.utf8))
    default:
        respond(conn, status: "404 Not Found", body: Data("{}".utf8))
    }
}

// Extract the bytes of a multipart part named `name`.
private func extractPart(_ body: Data, boundary: String, name: String) -> Data? {
    let delim = Data("--\(boundary)".utf8)
    var idx = body.startIndex
    var ranges: [Range<Data.Index>] = []
    while let r = body.range(of: delim, in: idx..<body.endIndex) {
        ranges.append(r); idx = r.upperBound
    }
    guard ranges.count >= 2 else { return nil }
    for i in 0..<(ranges.count - 1) {
        let partStart = ranges[i].upperBound
        let partEnd = ranges[i + 1].lowerBound
        let part = body[partStart..<partEnd]
        guard let hEnd = part.range(of: Data("\r\n\r\n".utf8)) else { continue }
        let hdr = String(data: part[..<hEnd.lowerBound], encoding: .utf8) ?? ""
        if hdr.contains("name=\"\(name)\"") {
            // content is between hEnd and the trailing \r\n before the next boundary
            var content = part[hEnd.upperBound...]
            if content.count >= 2 { content = content.dropLast(2) }  // trailing \r\n
            return Data(content)
        }
    }
    return nil
}

private func escapeJSON(_ s: String) -> String {
    "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
}
