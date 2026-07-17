#include "ort_shim.h"
#include <onnxruntime_c_api.h>
#include <stdlib.h>
#include <string.h>

static const OrtApi *g = NULL;

typedef struct {
    OrtEnv *env;
    OrtSession *session;
    OrtSessionOptions *opts;
    char *in_name;
    char *out_name;
} OrtHandle;

void *ort_load(const char *model_path) {
    if (!g) g = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    OrtHandle *h = calloc(1, sizeof(OrtHandle));
    if (!h) return NULL;
    if (g->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "immich-ml", &h->env)) goto fail;
    g->CreateSessionOptions(&h->opts);
    g->SetIntraOpNumThreads(h->opts, 1);
    g->SetSessionGraphOptimizationLevel(h->opts, ORT_ENABLE_ALL);
    if (g->CreateSession(h->env, model_path, h->opts, &h->session)) goto fail;
    OrtAllocator *alloc;
    g->GetAllocatorWithDefaultOptions(&alloc);
    g->SessionGetInputName(h->session, 0, alloc, &h->in_name);
    g->SessionGetOutputName(h->session, 0, alloc, &h->out_name);
    return h;
fail:
    ort_free(h);
    return NULL;
}

int ort_run(void *handle, const float *input, int64_t *shape, int ndim,
            float *output, int max_out) {
    OrtHandle *h = (OrtHandle *)handle;
    if (!h || !g) return -1;

    OrtMemoryInfo *mem = NULL;
    g->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &mem);

    size_t n = 1;
    for (int i = 0; i < ndim; i++) n *= (size_t)shape[i];

    OrtValue *in_t = NULL;
    if (g->CreateTensorWithDataAsOrtValue(mem, (void *)input, n * sizeof(float), shape,
                                          (size_t)ndim, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT,
                                          &in_t) || !in_t) {
        if (mem) g->ReleaseMemoryInfo(mem);
        return -1;
    }

    const char *in_names[] = {h->in_name};
    const char *out_names[] = {h->out_name};
    OrtValue *out_t = NULL;
    OrtStatus *st = g->Run(h->session, NULL, in_names, (const OrtValue *const *)&in_t, 1,
                           out_names, 1, &out_t);

    int count = -1;
    if (!st && out_t) {
        OrtTensorTypeAndShapeInfo *info = NULL;
        g->GetTensorTypeAndShape(out_t, &info);
        size_t c = 0;
        g->GetTensorShapeElementCount(info, &c);
        float *od = NULL;
        g->GetTensorMutableData(out_t, (void **)&od);
        count = (int)c;
        if (count > max_out) count = max_out;
        memcpy(output, od, (size_t)count * sizeof(float));
        g->ReleaseTensorTypeAndShapeInfo(info);
    } else if (st) {
        g->ReleaseStatus(st);
    }

    if (in_t) g->ReleaseValue(in_t);
    if (out_t) g->ReleaseValue(out_t);
    if (mem) g->ReleaseMemoryInfo(mem);
    return count;
}

static ONNXTensorElementDataType shim_type(int t) {
    switch (t) {
    case 1: return ONNX_TENSOR_ELEMENT_DATA_TYPE_INT32;
    case 2: return ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64;
    default: return ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT;
    }
}

static size_t shim_elem_size(int t) { return t == 2 ? 8 : 4; }

int ort_run_multi(void *handle, int n_inputs, const void **datas,
                  const int *elem_types, const int64_t *shapes,
                  const int *ndims, float *output, int max_out) {
    OrtHandle *h = (OrtHandle *)handle;
    if (!h || !g || n_inputs < 1 || n_inputs > 8) return -1;

    OrtMemoryInfo *mem = NULL;
    g->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault, &mem);

    OrtAllocator *alloc;
    g->GetAllocatorWithDefaultOptions(&alloc);

    // Bind values to the session's inputs in declaration order.
    const char *names[8];
    char *owned[8] = {0};
    OrtValue *vals[8] = {0};
    const int64_t *sp = shapes;
    int ok = 1;
    for (int i = 0; i < n_inputs; i++) {
        g->SessionGetInputName(h->session, (size_t)i, alloc, &owned[i]);
        names[i] = owned[i];
        size_t n = 1;
        for (int d = 0; d < ndims[i]; d++) n *= (size_t)sp[d];
        if (g->CreateTensorWithDataAsOrtValue(mem, (void *)datas[i],
                                              n * shim_elem_size(elem_types[i]), sp,
                                              (size_t)ndims[i], shim_type(elem_types[i]),
                                              &vals[i])) ok = 0;
        sp += ndims[i];
    }

    int count = -1;
    if (ok) {
        OrtValue *out_t = NULL;
        OrtStatus *st = g->Run(h->session, NULL, names, (const OrtValue *const *)vals,
                               (size_t)n_inputs, (const char *const *)&h->out_name, 1, &out_t);
        if (!st && out_t) {
            OrtTensorTypeAndShapeInfo *info = NULL;
            g->GetTensorTypeAndShape(out_t, &info);
            size_t c = 0;
            g->GetTensorShapeElementCount(info, &c);
            float *od = NULL;
            g->GetTensorMutableData(out_t, (void **)&od);
            count = (int)c;
            if (count > max_out) count = max_out;
            memcpy(output, od, (size_t)count * sizeof(float));
            g->ReleaseTensorTypeAndShapeInfo(info);
            g->ReleaseValue(out_t);
        } else if (st) {
            g->ReleaseStatus(st);
        }
    }
    for (int i = 0; i < n_inputs; i++) {
        if (vals[i]) g->ReleaseValue(vals[i]);
        // Input-name strings come from the ORT allocator; free them or every
        // request leaks a few strings.
        if (owned[i]) g->AllocatorFree(alloc, owned[i]);
    }
    if (mem) g->ReleaseMemoryInfo(mem);
    return count;
}

int ort_input_count(void *handle) {
    OrtHandle *h = (OrtHandle *)handle;
    if (!h || !g) return -1;
    size_t n = 0;
    g->SessionGetInputCount(h->session, &n);
    return (int)n;
}

int ort_input_elem_type(void *handle, int i) {
    OrtHandle *h = (OrtHandle *)handle;
    if (!h || !g) return -1;
    OrtTypeInfo *ti = NULL;
    if (g->SessionGetInputTypeInfo(h->session, (size_t)i, &ti) || !ti) return -1;
    const OrtTensorTypeAndShapeInfo *tsi = NULL;
    g->CastTypeInfoToTensorInfo(ti, &tsi);
    ONNXTensorElementDataType t = ONNX_TENSOR_ELEMENT_DATA_TYPE_UNDEFINED;
    if (tsi) g->GetTensorElementType(tsi, &t);
    g->ReleaseTypeInfo(ti);
    switch (t) {
    case ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT: return 0;
    case ONNX_TENSOR_ELEMENT_DATA_TYPE_INT32: return 1;
    case ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64: return 2;
    default: return -1;
    }
}

void ort_free(void *handle) {
    OrtHandle *h = (OrtHandle *)handle;
    if (!h) return;
    if (h->session) g->ReleaseSession(h->session);
    if (h->opts) g->ReleaseSessionOptions(h->opts);
    if (h->env) g->ReleaseEnv(h->env);
    free(h);
}
