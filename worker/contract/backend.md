# Backend components v1

Compiler.prepare(job_dir, request) produces source/compiled artifacts bound to source,
request and environment. K3: real Torch/FlagGems JIT invocation; K1: native C compiler.
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
is isolated in each job directory. K1 v1 supports native C add/abs and float32; no Torch claim.
Compiler/evaluator processes use existing process-group kill/reap on timeout.
Every result includes actual backend/environment/source identity and SHA-256 artifacts.
