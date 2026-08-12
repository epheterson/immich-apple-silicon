import Foundation

// Metal GPU lock for serializing MLX inference — Swift-side equivalent of
// ml/src/gpu_lock.py's metal_lock. MLX uses Metal command buffers with lazy
// evaluation; if an MLX array hasn't finished evaluating when a concurrent
// Vision-framework call (OCR/face-detect, also Metal-backed) submits its own
// Metal work on another thread, the command buffers collide. Server.swift
// accepts connections on a concurrent queue, so this race is live across
// simultaneous /predict requests. Uncaught, it crashes the whole process —
// this is what produced the "[METAL] Command buffer execution failed:
// Insufficient Memory" abort in production.
//
// Held around every MLX call site (CLIP.swift, CLIPText.swift,
// SigLIPNative.swift) through eval() completion, so no other Metal
// submission can land mid-inference. Vision framework (Vision.swift,
// OCR.swift, FaceDetect.swift) and the CoreML/ONNX Runtime path (ZooCLIP's
// non-native branch) use their own separate Metal command queues and do NOT
// need this lock — matching gpu_lock.py's documented scope.
let metalLock = NSLock()

@discardableResult
func withMetalLock<T>(_ body: () throws -> T) rethrows -> T {
    metalLock.lock()
    defer { metalLock.unlock() }
    return try body()
}
