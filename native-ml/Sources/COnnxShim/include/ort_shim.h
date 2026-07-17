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

// Generic run for models with N inputs of mixed types (the CLIP zoo towers).
// Element types: 0 = float32, 1 = int32, 2 = int64. Inputs are bound to the
// session's inputs in declaration order. Writes the FIRST output (float32) to
// `output`; returns element count or -1.
int ort_run_multi(void *handle, int n_inputs, const void **datas,
                  const int *elem_types, const int64_t *shapes,
                  const int *ndims, float *output, int max_out);

// Introspection: number of inputs; element type of input i (0/1/2 as above,
// -1 unknown).
int ort_input_count(void *handle);
int ort_input_elem_type(void *handle, int i);

void ort_free(void *handle);

#endif
