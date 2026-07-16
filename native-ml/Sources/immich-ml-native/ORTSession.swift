import Foundation
import COnnxShim

// Swift wrapper over the onnxruntime C shim. Runs the identical .onnx the Python
// service uses (same engine) so face embeddings match by construction.
final class ORTSession {
    private let handle: UnsafeMutableRawPointer
    let outDim: Int

    init?(modelPath: String, outDim: Int = 512) {
        guard let h = ort_load(modelPath) else { return nil }
        handle = h
        self.outDim = outDim
    }

    // Run one tensor (row-major float) through the model; returns the output row.
    func run(_ input: [Float], shape: [Int64]) -> [Float]? {
        var out = [Float](repeating: 0, count: outDim)
        var sh = shape
        let n = input.withUnsafeBufferPointer { inp in
            sh.withUnsafeMutableBufferPointer { sp in
                out.withUnsafeMutableBufferPointer { op in
                    ort_run(handle, inp.baseAddress, sp.baseAddress, Int32(shape.count),
                            op.baseAddress, Int32(outDim))
                }
            }
        }
        return n > 0 ? Array(out.prefix(Int(n))) : nil
    }

    deinit { ort_free(handle) }
}
