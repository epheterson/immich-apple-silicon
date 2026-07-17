import Foundation

// CLIP byte-level BPE tokenizer. Byte-for-byte match to mlx_clip / open_clip:
// SOT=49406, EOT=49407, context length 77. Loads vocab.json + merges.txt from
// the model dir (same files mlx-community ships). ASCII-clean only (no ftfy);
// search queries are ASCII so basic_clean is identity for our inputs.
final class CLIPTokenizer {
    static let SOT = 49406, EOT = 49407, CTX = 77

    private let encoder: [String: Int]        // subword token -> id
    private let bpeRanks: [String: Int]       // "a b" -> merge rank
    private let byteEncoder: [UInt8: Character]
    private let pat: NSRegularExpression

    init(modelDir: String) {
        let vocabData = try! Data(contentsOf: URL(fileURLWithPath: modelDir + "/vocab.json"))
        encoder = try! JSONSerialization.jsonObject(with: vocabData) as! [String: Int]

        let mergesText = try! String(contentsOfFile: modelDir + "/merges.txt", encoding: .utf8)
        var ranks: [String: Int] = [:]
        var rank = 0
        for (idx, line) in mergesText.split(separator: "\n").enumerated() {
            if idx == 0 && line.hasPrefix("#") { continue }   // "#version: 0.2" header
            let l = line.trimmingCharacters(in: .whitespaces)
            if l.isEmpty { continue }
            ranks[l] = rank; rank += 1
        }
        bpeRanks = ranks

        byteEncoder = Self.bytesToUnicode()
        // CLIP's token pattern (case-insensitive): contractions, letter runs,
        // single digits, and non-space/letter/digit runs.
        pat = try! NSRegularExpression(
            pattern: "<\\|startoftext\\|>|<\\|endoftext\\|>|'s|'t|'re|'ve|'m|'ll|'d|\\p{L}+|\\p{N}|[^\\s\\p{L}\\p{N}]+",
            options: [.caseInsensitive])
    }

    // Reversible byte<->unicode map so every byte is a printable char (GPT-2/CLIP).
    static func bytesToUnicode() -> [UInt8: Character] {
        var bs: [Int] = Array(33...126) + Array(161...172) + Array(174...255)
        var cs = bs
        var n = 0
        for b in 0..<256 where !bs.contains(b) {
            bs.append(b); cs.append(256 + n); n += 1
        }
        var map: [UInt8: Character] = [:]
        for (b, c) in zip(bs, cs) { map[UInt8(b)] = Character(UnicodeScalar(c)!) }
        return map
    }

    private func whitespaceClean(_ s: String) -> String {
        s.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    private func getPairs(_ word: [String]) -> Set<[String]> {
        var pairs = Set<[String]>()
        for i in 0..<(word.count - 1) { pairs.insert([word[i], word[i + 1]]) }
        return pairs
    }

    // Byte-pair-encode one whitespace-free token into subword units.
    private func bpe(_ token: String) -> [String] {
        var word = token.map { String($0) }
        guard !word.isEmpty else { return [] }
        word[word.count - 1] += "</w>"
        if word.count == 1 { return word }
        var pairs = getPairs(word)
        if pairs.isEmpty { return word }

        while true {
            var bigram: [String]? = nil
            var bestRank = Int.max
            for p in pairs {
                let r = bpeRanks[p[0] + " " + p[1]] ?? Int.max
                if r < bestRank { bestRank = r; bigram = p }
            }
            guard let bg = bigram, bestRank != Int.max else { break }
            let first = bg[0], second = bg[1]
            var newWord: [String] = []
            var i = 0
            while i < word.count {
                if let j = word[i...].firstIndex(of: first) {
                    newWord.append(contentsOf: word[i..<j])
                    i = j
                } else {
                    newWord.append(contentsOf: word[i...])
                    break
                }
                if word[i] == first && i < word.count - 1 && word[i + 1] == second {
                    newWord.append(first + second); i += 2
                } else {
                    newWord.append(word[i]); i += 1
                }
            }
            word = newWord
            if word.count == 1 { break }
            pairs = getPairs(word)
        }
        return word
    }

    func encode(_ text: String) -> [Int] {
        let cleaned = whitespaceClean(text).lowercased()
        var tokens: [Int] = [Self.SOT]
        let ns = cleaned as NSString
        for m in pat.matches(in: cleaned, range: NSRange(location: 0, length: ns.length)) {
            let piece = ns.substring(with: m.range)
            var encoded = ""
            for b in Array(piece.utf8) { encoded.append(byteEncoder[b]!) }
            for sub in bpe(encoded) where encoder[sub] != nil {
                tokens.append(encoder[sub]!)
            }
        }
        tokens.append(Self.EOT)
        return tokens
    }
}
