#ifndef ORT_SHIM_H
#define ORT_SHIM_H
#include <stdint.h>

// Minimal C wrapper over onnxruntime's C API, called from Swift. Keeps the
// verbose OrtApi function-pointer dance in C where it reads naturally.

// Load an ONNX model (CPU provider, single intra-op thread). Returns an opaque
// handle, or NULL on failure.
void *ort_load(const char *model_path);

// Run a single float tensor through the model. `shape`/`ndim` describe the input
// (e.g. [1,3,112,112]). Writes up to `max_out` floats to `output`. Returns the
// number of output elements, or -1 on failure.
int ort_run(void *handle, const float *input, int64_t *shape, int ndim,
            float *output, int max_out);

void ort_free(void *handle);

#endif
