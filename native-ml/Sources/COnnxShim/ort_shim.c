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
    g->CreateTensorWithDataAsOrtValue(mem, (void *)input, n * sizeof(float), shape,
                                      (size_t)ndim, ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &in_t);

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

void ort_free(void *handle) {
    OrtHandle *h = (OrtHandle *)handle;
    if (!h) return;
    if (h->session) g->ReleaseSession(h->session);
    if (h->opts) g->ReleaseSessionOptions(h->opts);
    if (h->env) g->ReleaseEnv(h->env);
    free(h);
}
