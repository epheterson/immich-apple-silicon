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

    // Typed input for multi-input models (the CLIP zoo towers).
    enum Tensor {
        case float([Float], shape: [Int64])
        case int32([Int32], shape: [Int64])
        case int64([Int64], shape: [Int64])
    }

    // Element type (0=float32, 1=int32, 2=int64) of input `i`, per the model.
    func inputElemType(_ i: Int) -> Int { Int(ort_input_elem_type(handle, Int32(i))) }

    // Run a model with typed inputs bound in declaration order; returns the
    // first output as floats (embedding models have a single output).
    func runMulti(_ inputs: [Tensor], outDim: Int) -> [Float]? {
        var out = [Float](repeating: 0, count: outDim)
        var types: [Int32] = []
        var ndims: [Int32] = []
        var shapes: [Int64] = []
        // Keep buffers alive across the C call.
        var holds: [Any] = []
        var ptrs: [UnsafeRawPointer?] = []
        for input in inputs {
            switch input {
            case .float(let v, let s):
                let buf = UnsafeMutableBufferPointer<Float>.allocate(capacity: v.count)
                _ = buf.initialize(from: v)
                holds.append(buf); ptrs.append(UnsafeRawPointer(buf.baseAddress))
                types.append(0); ndims.append(Int32(s.count)); shapes.append(contentsOf: s)
            case .int32(let v, let s):
                let buf = UnsafeMutableBufferPointer<Int32>.allocate(capacity: v.count)
                _ = buf.initialize(from: v)
                holds.append(buf); ptrs.append(UnsafeRawPointer(buf.baseAddress))
                types.append(1); ndims.append(Int32(s.count)); shapes.append(contentsOf: s)
            case .int64(let v, let s):
                let buf = UnsafeMutableBufferPointer<Int64>.allocate(capacity: v.count)
                _ = buf.initialize(from: v)
                holds.append(buf); ptrs.append(UnsafeRawPointer(buf.baseAddress))
                types.append(2); ndims.append(Int32(s.count)); shapes.append(contentsOf: s)
            }
        }
        defer {
            for h in holds {
                if let b = h as? UnsafeMutableBufferPointer<Float> { b.deallocate() }
                if let b = h as? UnsafeMutableBufferPointer<Int32> { b.deallocate() }
                if let b = h as? UnsafeMutableBufferPointer<Int64> { b.deallocate() }
            }
        }
        let n = ptrs.withUnsafeMutableBufferPointer { pp in
            types.withUnsafeBufferPointer { tp in
                shapes.withUnsafeBufferPointer { sp in
                    ndims.withUnsafeBufferPointer { np in
                        out.withUnsafeMutableBufferPointer { op in
                            ort_run_multi(handle, Int32(inputs.count), pp.baseAddress,
                                          tp.baseAddress, sp.baseAddress, np.baseAddress,
                                          op.baseAddress, Int32(outDim))
                        }
                    }
                }
            }
        }
        return n > 0 ? Array(out.prefix(Int(n))) : nil
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
