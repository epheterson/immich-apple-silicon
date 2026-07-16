// Native ArcFace runner: onnxruntime C++ API on w600k_r50.onnx.
// Proves the InsightFace face-embedding model runs natively (no Python/venv)
// with exact parity vs the Python onnxruntime path — same engine, same weights.
// A Swift binary links the identical libonnxruntime via its C ABI.
#include <onnxruntime_cxx_api.h>
#include <cstdio>
#include <cmath>
#include <vector>
#include <string>

int main(int argc, char** argv) {
  const char* model = argv[1];                     // .../w600k_r50.onnx
  const char* inpath = argv[2];                    // /tmp/arcface_input.f32
  const int N = 112, C = 3, SZ = C * N * N;

  std::vector<float> in(SZ);
  FILE* f = fopen(inpath, "rb");
  if (!f) { printf("cannot open %s\n", inpath); return 1; }
  fread(in.data(), sizeof(float), SZ, f); fclose(f);

  Ort::Env env(ORT_LOGGING_LEVEL_WARNING, "arcface");
  Ort::SessionOptions opts;
  opts.SetIntraOpNumThreads(1);
  Ort::Session sess(env, model, opts);

  Ort::AllocatorWithDefaultOptions alloc;
  auto inName = sess.GetInputNameAllocated(0, alloc);
  auto outName = sess.GetOutputNameAllocated(0, alloc);
  const char* inNames[]  = { inName.get() };
  const char* outNames[] = { outName.get() };

  int64_t shape[4] = {1, C, N, N};
  auto mem = Ort::MemoryInfo::CreateCpu(OrtArenaAllocator, OrtMemTypeDefault);
  Ort::Value tensor = Ort::Value::CreateTensor<float>(mem, in.data(), SZ, shape, 4);

  auto outs = sess.Run(Ort::RunOptions{nullptr}, inNames, &tensor, 1, outNames, 1);
  float* emb = outs[0].GetTensorMutableData<float>();
  int dim = (int)outs[0].GetTensorTypeAndShapeInfo().GetElementCount();

  double l2 = 0; for (int i = 0; i < dim; i++) l2 += (double)emb[i] * emb[i];
  printf("EMB dim=%d L2=%.4f\n", dim, sqrt(l2));
  printf("first6: [%.5f, %.5f, %.5f, %.5f, %.5f, %.5f]\n",
         emb[0], emb[1], emb[2], emb[3], emb[4], emb[5]);
  FILE* o = fopen("/tmp/arcface_native_emb.f32", "wb");
  fwrite(emb, sizeof(float), dim, o); fclose(o);
  return 0;
}
