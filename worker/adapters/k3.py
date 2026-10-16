"""Isolated K3 compiler/evaluator. Candidate exports candidate(*inputs). No installed code edits."""
import importlib.util
import hashlib
import inspect
import json
import os
import pathlib
import statistics
import sys
import time

import torch
import torch.nn.functional as F
import flag_gems
import triton

r = json.loads(pathlib.Path("request.json").read_text())
torch.manual_seed(r["seed"])
torch.set_num_threads(1)
dtype = getattr(torch, r["dtype"])
a = torch.randn(r["shape"], dtype=dtype)
b = torch.randn_like(a)
op = r["op"]
inputs = [a]
if op in {"add", "sub", "mul", "div"}:
    if op == "div":
        b = b.abs() + 0.5
    inputs = [a, b]
    ref = lambda: getattr(torch, op)(*inputs)
    candidate = lambda *xs: getattr(flag_gems, op)(*xs)
elif op in {"abs", "exp", "relu", "sum"}:
    ref = lambda: getattr(torch, op)(a)
    candidate = lambda x: getattr(flag_gems, op)(x)
elif op == "softmax":
    ref = lambda: torch.softmax(a, dim=-1)
    candidate = lambda x: flag_gems.softmax(x, -1)
elif op in {"mm", "mv"}:
    b = torch.randn((a.shape[1], a.shape[1]) if op == "mm" else (a.shape[1],), dtype=dtype)
    inputs = [a, b]
    ref = lambda: getattr(torch, op)(a, b)
    candidate = lambda *xs: getattr(flag_gems, op)(*xs)
elif op == "aminmax":
    ref = lambda: torch.aminmax(a)
    candidate = lambda x: flag_gems.aminmax(x)
elif op == "topk":
    k = min(5, a.shape[-1])
    ref = lambda: torch.topk(a, k, dim=-1)
    candidate = lambda x: flag_gems.topk(x, k, -1)
elif op == "conv2d":
    w = torch.randn((a.shape[1], a.shape[1], 3, 3), dtype=dtype)
    inputs = [a, w]
    ref = lambda: F.conv2d(a, w, padding=1)
    candidate = lambda x, y: flag_gems.conv2d(x, y, padding=1)
else:
    raise ValueError("unsupported operation")

if pathlib.Path("candidate.py").exists():
    spec = importlib.util.spec_from_file_location("rvbench_candidate", "candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate = module.candidate

fn = lambda: candidate(*inputs)
started = time.perf_counter()
actual = fn()  # Real JIT/smoke. Never included in timing samples.
compile_ms = (time.perf_counter() - started) * 1000
if sys.argv[1] == "prepare":
    implementation = module.candidate if pathlib.Path("candidate.py").exists() else getattr(flag_gems, op)
    source_path = inspect.getsourcefile(inspect.unwrap(implementation))
    source = pathlib.Path(source_path).read_bytes() if source_path else b""
    pathlib.Path("implementation.py").write_bytes(source)
    pathlib.Path("prepare.json").write_text(json.dumps({"torch": torch.__version__, "triton": triton.__version__, "smoke_ms": compile_ms, "shape": r["shape"], "op": op, "implementation_sha256": hashlib.sha256(source).hexdigest(), "implementation_path": source_path, "cpus": sorted(os.sched_getaffinity(0))}))
    print(json.dumps({"prepared": True}))
    sys.exit(0)

if r["kind"] in ("correctness", "performance"):
    expected = ref()
    actuals = list(actual) if isinstance(actual, tuple) else [actual]
    expecteds = list(expected) if isinstance(expected, tuple) else [expected]
    atol, rtol = (0.05, 0.05) if dtype == torch.float16 else (0.005, 0.005)
    checks = [x.shape == y.shape and (torch.allclose(x, y, atol=atol, rtol=rtol) if x.is_floating_point() else torch.equal(x, y)) for x, y in zip(actuals, expecteds)]
    errors = [(x.float() - y.float()).abs().max().item() for x, y in zip(actuals, expecteds) if x.shape == y.shape]
    validation = {"correct": len(actuals) == len(expecteds) and all(checks), "max_abs_error": max(errors, default=0), "atol": atol, "rtol": rtol, "reference": "pytorch-native", "scope": "submitted-case", "seed": r["seed"]}
    if r["kind"] == "correctness" or not validation["correct"]:
        print(json.dumps(validation, allow_nan=False))
        sys.exit(0)
if r["kind"] == "performance":
    for _ in range(r["warmup"]):
        fn()
    def measure(f):
        samples = []
        for _ in range(r["repetitions"]):
            start = time.perf_counter_ns()
            out = f()
            samples.append((time.perf_counter_ns() - start) / 1e6)
            del out
        return samples
    samples = measure(fn)
    for _ in range(r["warmup"]):
        ref()
    reference = measure(ref)
    print(json.dumps({**validation, "samples_ms": samples, "median_ms": statistics.median(samples), "reference_samples_ms": reference, "reference_median_ms": statistics.median(reference), "reference": "pytorch-native", "warmup": r["warmup"], "repetitions": r["repetitions"], "clock": "perf_counter_ns", "compilation_excluded": True}, allow_nan=False))
