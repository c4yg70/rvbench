#!/usr/bin/env python
"""X100 预编译桥接（板上运行，由 Rust rvbench-tools precompile 上传）。

argv: <candidate.py> <op> <dtype> <shapes-json>
独立进程 import 候选模块（顶层模块，非部署）→ 按 bench_op 直调层顺序解析入口 →
逐 shape 构造目标输入 → use_gems() 下调用一次触发 triton JIT（写共享缓存）。
FAIL 退出码 0：不阻塞主流程，调用方读输出决定是否仍提交作业。
"""
import importlib.util
import inspect
import json
import os
import re
import sys
import time
from contextlib import nullcontext

DTYPES = {"float32": "float32", "float16": "float16", "int32": "int32",
          "int64": "int64", "bool": "bool"}

if len(sys.argv) != 5:
    sys.stderr.write("usage: _precompile_driver.py <candidate.py> <op> <dtype> <shapes-json>\n")
    raise SystemExit(2)
CAND_PATH, OP, DTYPE_STR, SHAPES_JSON = sys.argv[1:5]


def _cache_root():
    return os.environ.get("TRITON_CACHE_DIR") or os.path.expanduser("~/.triton/cache")


def _cache_stat():
    root = _cache_root()
    entries = files = 0
    if os.path.isdir(root):
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p) and not os.path.islink(p):
                entries += 1
                for _d, _dn, fs in os.walk(p):
                    files += len(fs)
    return {"root": root, "entries": entries, "files": files}


def _allowed_cpus():
    try:
        for line in open("/proc/self/status"):
            if line.startswith("Cpus_allowed_list"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _fail(reason):
    print(f"PRECOMPILE_FAIL {OP} {reason}")
    print("PRECOMPILE_JSON " + json.dumps(
        {"ok": False, "op": OP, "candidate": CAND_PATH, "reason": str(reason)[:500],
         "cpus_allowed": _allowed_cpus()}, ensure_ascii=False))
    sys.stdout.flush()
    raise SystemExit(0)


def _main():
    t_wall = time.perf_counter()
    if not os.path.isfile(CAND_PATH):
        _fail(f"候选文件不存在: {CAND_PATH}")
    if DTYPE_STR not in DTYPES:
        _fail(f"dtype 不支持: {DTYPE_STR}")
    if not re.match(r"^[a-z_][a-z0-9_]{0,47}$", OP):
        _fail(f"op 不合法: {OP}")
    try:
        raw_shapes = json.loads(SHAPES_JSON)
        shapes = [tuple(int(x) for x in str(s).split(",")) for s in raw_shapes]
    except (ValueError, TypeError):
        _fail(f"shape 不合法: {SHAPES_JSON[:200]}")
    if not shapes:
        _fail("shape 列表为空")

    stat0 = _cache_stat()  # 须在 import torch/triton 之前计数

    import torch  # noqa: E402
    import flag_gems  # noqa: E402

    stem = os.path.splitext(os.path.basename(CAND_PATH))[0]
    modname = "k3_precompile_cand_" + re.sub(r"\W", "_", stem)
    spec = importlib.util.spec_from_file_location(modname, CAND_PATH)
    if spec is None or spec.loader is None:
        _fail(f"无法构造 import spec: {CAND_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 候选自身语法/导入错 -> FAIL（不阻塞）
        _fail(f"候选模块 import 失败: {type(exc).__name__}: {exc}")

    entry = entry_name = None
    for name in (OP, OP + "_func", OP + "_forward", OP + "_run", OP + "_launch"):
        fn = getattr(mod, name, None)
        if callable(fn):
            entry, entry_name = fn, name
            break
    if entry is None:
        _fail(f"模块内未解析到可调用入口（试过 {OP}/{OP}_func/{OP}_forward/{OP}_run/{OP}_launch）")

    dt = {"float32": torch.float32, "float16": torch.float16, "int32": torch.int32,
          "int64": torch.int64, "bool": torch.bool}[DTYPE_STR]

    def make_input(sh, d):
        if d.is_floating_point:
            return torch.randn(sh, dtype=d)
        if d == torch.bool:
            return torch.rand(sh) > 0.5
        return torch.randint(-100, 100, sh, dtype=d)

    try:
        n_req = sum(1 for p in inspect.signature(entry).parameters.values()
                    if p.default is inspect.Parameter.empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    except (TypeError, ValueError):
        n_req = 1
    if n_req > 3:
        _fail(f"入口必选参数 {n_req} 个 >3，超出驱动能力")

    gems_ctx = getattr(flag_gems, "use_gems", None)
    per = []
    for sh in shapes:
        rec = {"shape": ",".join(str(x) for x in sh)}
        torch.manual_seed(0)
        t0 = time.perf_counter()
        ref_args = out = None
        try:
            a = make_input(sh, dt)
            with (gems_ctx() if callable(gems_ctx) else nullcontext()):
                if n_req <= 1:
                    out, ref_args = entry(a), (a,)
                elif n_req == 2:
                    b = make_input(sh, dt)
                    try:
                        out, ref_args = entry(a, b), (a, b)
                    except Exception:  # noqa: BLE001 归约类 dim 参数试探（bench_op 同款）
                        out, ref_args = entry(a, -1), (a, -1)
                else:
                    cond = torch.rand(sh) > 0.5
                    out, ref_args = entry(cond, a, a), (cond, a, a)
            rec["ok"] = True
            rec["call_s"] = round(time.perf_counter() - t0, 3)
        except Exception as exc:  # noqa: BLE001 单 shape 失败不牵连其余
            rec["ok"] = False
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            rec["call_s"] = round(time.perf_counter() - t0, 3)
            per.append(rec)
            continue
        # 数值对照尽力而为（只为给调用方一个早期信号，不阻塞预编译结论）
        try:
            t_op = getattr(torch, OP, None)
            if callable(t_op) and ref_args is not None and isinstance(out, torch.Tensor):
                ref = t_op(*ref_args)
                if isinstance(ref, torch.Tensor) and ref.shape == out.shape:
                    rt = at = 5e-2 if dt == torch.float16 else 5e-3
                    rec["correct"] = bool(torch.allclose(out.float(), ref.float(), rtol=rt, atol=at))
                    rec["max_abs_err"] = round(float((out.float() - ref.float()).abs().max()), 6)
        except Exception:  # noqa: BLE001
            pass
        per.append(rec)

    stat1 = _cache_stat()
    n_ok = sum(1 for r in per if r.get("ok"))
    rec_out = {"ok": n_ok > 0, "op": OP, "candidate": CAND_PATH, "entry": entry_name,
               "dtype": DTYPE_STR, "arity": n_req,
               "shapes": [r["shape"] for r in per], "n_shapes": len(per),
               "n_ok": n_ok, "n_failed": len(per) - n_ok, "per_shape": per,
               "total_s": round(time.perf_counter() - t_wall, 1),
               "cache_before": stat0, "cache_after": stat1,
               "cache_entries_delta": stat1["entries"] - stat0["entries"],
               "cache_files_delta": stat1["files"] - stat0["files"],
               "cpus_allowed": _allowed_cpus()}
    if n_ok == 0:
        first_err = next((r.get("error") for r in per if r.get("error")), "未知")
        print(f"PRECOMPILE_FAIL {OP} 全部 {len(per)} 个 shape 调用失败: {first_err}")
    else:
        print(f"PRECOMPILE_OK {OP} shapes={len(per)} ok={n_ok} failed={len(per) - n_ok} "
              f"total_s={rec_out['total_s']} "
              f"cache_entries {stat0['entries']}->{stat1['entries']} (+{rec_out['cache_entries_delta']}) "
              f"cache_files {stat0['files']}->{stat1['files']} (+{rec_out['cache_files_delta']}) "
              f"entry={entry_name} cpus={rec_out['cpus_allowed']}")
    print("PRECOMPILE_JSON " + json.dumps(rec_out, ensure_ascii=False))
    sys.stdout.flush()
    raise SystemExit(0)


try:
    _main()
except SystemExit:
    raise
except Exception as exc:  # noqa: BLE001 一切异常 -> FAIL（不阻塞主流程）
    _fail(f"{type(exc).__name__}: {exc}")
