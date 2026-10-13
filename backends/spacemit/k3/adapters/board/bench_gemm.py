#!/usr/bin/env python
"""GEMM 三算子绝对延迟微基准：torch 参考 vs flag_gems，绝对 ms + GFLOPS。

用法（先 source ~/autoke/scripts/run_env.sh）::

  python ~/autoke/scripts/bench_gemm.py --op mm --dtype float32 --shapes 384,512,1024,2048
  python ~/autoke/scripts/bench_gemm.py --op addmm --dtype float16 --shapes 384,1024
  python ~/autoke/scripts/bench_gemm.py --op bmm --dtype float16 --shapes 384 --batch 8

自适应 rep：单次调用 >30s 时 rep=1，避免大 shape 测量拖爆超时。
输出：每个 (op,dtype,shape) 一行 JSON。GFLOPS = 2*M*N*K / ms / 1e6。
"""

import argparse
import json
import time

import torch
import flag_gems

DTYPES = {"float32": torch.float32, "float16": torch.float16}


def bench(fn, min_rep=2, max_rep=20, target_s=3.0):
    """median of reps; rep 自适应：单次太慢就少测几次。"""
    t0 = time.perf_counter()
    fn()
    one = time.perf_counter() - t0
    nw = 2 if one < 1.0 else 1
    for _ in range(nw):
        fn()
    if one > 30.0:
        rep = 1
    else:
        rep = max(min_rep, min(max_rep, int(target_s / max(one, 1e-9)) + 1))
    ts = []
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1e3)
    ts.sort()
    return ts[len(ts) // 2], rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True, choices=["mm", "addmm", "bmm"])
    ap.add_argument("--dtype", default="float32", choices=list(DTYPES))
    ap.add_argument("--shapes", default="384,512,1024,2048")
    ap.add_argument("--batch", type=int, default=8, help="bmm batch size")
    ap.add_argument("--threads", type=int, default=0, help="0=keep default")
    ap.add_argument("--skip-torch", action="store_true")
    ap.add_argument("--skip-gems", action="store_true")
    args = ap.parse_args()

    if args.threads > 0:
        torch.set_num_threads(args.threads)

    dtype = DTYPES[args.dtype]
    torch.manual_seed(0)
    out = []
    for s in [int(x) for x in args.shapes.split(",")]:
        if args.op == "bmm":
            a = torch.randn(args.batch, s, s, dtype=dtype)
            b = torch.randn(args.batch, s, s, dtype=dtype)
            bias = None
            flops = 2.0 * args.batch * s * s * s
        else:
            a = torch.randn(s, s, dtype=dtype)
            b = torch.randn(s, s, dtype=dtype)
            bias = torch.randn(1, s, dtype=dtype)  # addmm broadcast
            flops = 2.0 * s * s * s

        if args.op == "mm":
            tfn = lambda: torch.mm(a, b)
            gfn = lambda: flag_gems.mm(a, b)
        elif args.op == "addmm":
            tfn = lambda: torch.addmm(bias, a, b)
            gfn = lambda: flag_gems.addmm(bias, a, b)
        else:
            tfn = lambda: torch.bmm(a, b)
            gfn = lambda: flag_gems.bmm(a, b)

        torch_ms = float("nan")
        rep_t = 0
        if not args.skip_torch:
            torch_ms, rep_t = bench(tfn)
        gems_ms = float("nan")
        rep_g = 0
        if not args.skip_gems:
            gems_ms, rep_g = bench(gfn)

        # 宽松校验
        with torch.no_grad():
            r = tfn().float()
            g = gfn().float()
        ok = bool(torch.allclose(r, g, rtol=5e-2, atol=5e-2))
        max_err = float((r - g).abs().max())

        spd = torch_ms / gems_ms if gems_ms > 0 and torch_ms == torch_ms else None
        tg = round(flops / torch_ms / 1e6, 2) if torch_ms == torch_ms else None
        row = {
            "op": args.op, "dtype": args.dtype, "M": s, "N": s, "K": s,
            "batch": args.batch if args.op == "bmm" else 1,
            "threads": torch.get_num_threads(),
            "torch_ms": round(torch_ms, 3) if torch_ms == torch_ms else None,
            "gems_ms": round(gems_ms, 3) if gems_ms == gems_ms else None,
            "speedup": round(spd, 2) if spd else None,
            "torch_gflops": tg,
            "gems_gflops": round(flops / gems_ms / 1e6, 2) if gems_ms == gems_ms else None,
            "correct": ok, "max_abs_err": round(max_err, 4),
            "rep_t": rep_t, "rep_g": rep_g,
        }
        out.append(row)
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
