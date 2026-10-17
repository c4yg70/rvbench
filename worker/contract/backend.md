# Backend components v1

Compiler.prepare(job_dir, request) produces source/compiled artifacts bound to source,
request and environment. Both K1 and K3 support independent native-c and triton
implementations, selected by JobRequest.implementation. Omission retains the legacy
default (K1=native-c, K3=triton). native-c uses the configured cc; triton uses the
configured Python with Torch/Triton/FlagGems and a real JIT smoke invocation.
Evaluator.evaluate consumes prepared artifacts. Kinds: correctness / performance.
func invokes compiler plus a smoke invocation, never claims numerical correctness.
Correctness compares candidate outputs with the declared native reference.
Performance records real warmup/sample repetitions; compile/preparation are excluded.
Performance first verifies the actual outputs on its execution board, before any timing.
Failed numerical preflight returns evaluation failed with no timing samples. Successful
timings carry correct=true and compilation_excluded=true; this evidence gates ranking.
Optional phase_timings_seconds records compiler/evaluator/queue_wait wall durations.
Older missing timing/validation evidence remains unknown; it is never reconstructed as zero.
Supported operations and dtype are advertised explicitly; unsupported requests fail admission.
K3 uses installed Torch/FlagGems without modifying its installation tree. Candidate source
is isolated in each job directory. Native C supports add/abs and float32 on either
backend; Triton supports the existing K3 operation set and float32/float16 on either
backend. Native C ABI: void candidate(const float*, const float*, float*, int).
Triton Python entry point: candidate(*inputs), returning the declared operator output.
Worker environment.implementations maps native-c/triton to {ready, ops, dtypes,
probe_compilation} or {ready:false,error}. Each implementation is probed independently
in a fresh directory with real compilation and smoke execution. An unavailable runtime
does not disable the other runtime; startup fails if neither is usable. Top-level ops
and dtypes describe only the legacy default for old schedulers. New admission and
scheduling match implementation + op + dtype; legacy registrations without this map
support only their original default implementation and requests omitting the new field
(old HTTP schemas reject it). No unprobed runtime is advertised.
Compiler/evaluator processes use existing process-group kill/reap on timeout.
Every result includes actual backend/environment/source identity and SHA-256 artifacts.
