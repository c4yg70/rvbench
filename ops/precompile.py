#!/usr/bin/env python
"""X100 预编译（ADR-0012 / t4-runbook §8.2）—— pssh 通道 python 版。

取代 `ops/precompile_candidate.sh`：那份脚本用已废弃的 `bin/k3ssh`/`bin/k3scp`
（Windows ssh.exe 与 Git Bash ssh 双双失效，见 AGENTS.md），且是 bash，pwsh 下
跑不了。本版一律走 `ops/pssh.py`（paramiko；上传用 `--put`，走 SFTP）。

与 sh 版两处有意差异（吞吐提升计划 A1）：
1. **默认不启用 bench_op 回退**——回退路径 `flock /tmp/k3_bench.lock` 会与
   timing 车道抢锁、反噬吞吐；要开用 `--fallback`（并接受它堵计时车道）。
2. **一次进程编多 shape**——driver 接受 shape 列表，`import torch/flag_gems`
   只付一次（sh 版每 shape 一个进程，≈16s import 白付）。

流程：上传 driver+候选到板上 `~/autoke/tmp_precompile/` → X100 默认调度（不注册
A100、不碰计时锁）临时 python 进程 import 候选 → 按 bench_op 直调层顺序解析入口
（`<op>/<op>_func/<op>_forward/<op>_run/<op>_launch`）→ 逐 shape 构造输入 →
`use_gems()` 下调用一次触发 JIT 写共享缓存 → 打印契约行与缓存增量。

用法::

    python ops/precompile.py <候选文件> <op> <shape[,shape...]> <dtype> \
        [--board Home-SpacemitK3] [--timeout 300] [--fallback] [--keep]

输出契约（与 sh 版逐字兼容）：`PRECOMPILE_OK ...` / `PRECOMPILE_FAIL ...` /
`PRECOMPILE_JSON {...}`
退出码：0=OK 或 FAIL（FAIL 不阻塞主流程，调用方决定是否仍提交作业）；
        2=用法错误；4=板不可达/基础设施错误

也可作为模块用：`from precompile import precompile`（eval_pipeline 接入点）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PSSH = ROOT / "bin" / "pssh.py"
DEFAULT_BOARD = "Home-SpacemitK3"
TDIR = "/home/c4y/autoke/tmp_precompile"
RUN_ENV = "/home/c4y/autoke/scripts/run_env.sh"
BENCH_OP = "/home/c4y/autoke/scripts/bench_op.py"
LOCK_BASE = "/tmp/k3_bench.lock"   # 单车道锁；多车道时为 <LOCK_BASE>.<worker_index>（ADR-0014）
DRIVER_NAME = "_precompile_driver.py"

RE_OP = re.compile(r"^[a-z_][a-z0-9_]{0,47}$")
RE_SHAPE = re.compile(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")
RE_CAND = re.compile(r"^[A-Za-z0-9._-]+\.py$")
DTYPES = {"float32", "float16", "int32", "int64", "bool"}
MAX_SHAPES = 16          # 与 evald MAX_SHAPES 对齐（一次预编译不超过一个作业的 case 数）


class BoardUnreachable(RuntimeError):
    """板不可达/pssh 基础设施错误（退出码 4 语义）。"""


# ------------------------------------------------------------------ 板上 driver
# 每次随本脚本重新上传，防版本漂移（与 sh 版同一策略）。多 shape 变体：
# import 只付一次，逐 shape 独立成败（单 shape 崩不牵连其余）。
DRIVER_SRC = r'''#!/usr/bin/env python
"""X100 预编译驱动（板上运行，由 ops/precompile.py 生成并上传）。

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
'''


# ------------------------------------------------------------------ pssh 通道

def _pssh_python() -> str:
    """跑 pssh.py 的解释器：默认当前解释器（须有 paramiko），可用 env 覆盖。"""
    return os.environ.get("KOPE_PSSH_PYTHON") or sys.executable


def _run_pssh(args: list[str], timeout_s: int, cwd: str | None = None) -> tuple[int, str]:
    """调 ops/pssh.py，返回 (rc, 合并输出)。板不可达抛 BoardUnreachable。

    cwd 用于上传：pssh 的 `--put LOCAL:REMOTE` 按第一个冒号切分，Windows 盘符
    路径（c:\\...）会被切碎，故上传一律在临时目录内以**相对文件名**传。
    """
    cmd = [_pssh_python(), str(PSSH), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout_s + 60, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise BoardUnreachable(f"pssh 超时（{timeout_s + 60}s）: {' '.join(args[:3])}") from None
    out = (p.stdout or "") + (p.stderr or "")
    if "CONNECT_FAILED" in out or "EXEC_FAILED" in out:
        raise BoardUnreachable(out.strip()[:300])
    m = re.search(r"^EXIT=(-?\d+)$", out, re.M)
    return (int(m.group(1)) if m else p.returncode), out


def _put(board: str, local_name: str, remote: str, cwd: str, timeout_s: int = 60) -> None:
    rc, out = _run_pssh([board, "--put", f"{local_name}:{remote}", "--timeout", str(timeout_s)],
                        timeout_s, cwd=cwd)
    if rc != 0 or "PUT_OK" not in out:
        raise BoardUnreachable(f"上传失败 {local_name}->{remote}: {out.strip()[:300]}")


def _parse_contract(out: str) -> tuple[str | None, dict | None]:
    """从板上输出里取 PRECOMPILE_OK/FAIL 与 PRECOMPILE_JSON。"""
    verdict = None
    m = re.search(r"^PRECOMPILE_(OK|FAIL)\b", out, re.M)
    if m:
        verdict = m.group(1)
    detail = None
    for line in out.splitlines():
        if line.startswith("PRECOMPILE_JSON "):
            try:
                detail = json.loads(line[len("PRECOMPILE_JSON "):])
            except json.JSONDecodeError:
                detail = None
    return verdict, detail


# ------------------------------------------------------------------ 主入口

def precompile(candidate: str | os.PathLike, op: str, shapes: list[str], dtype: str,
               board: str = DEFAULT_BOARD, timeout_s: int = 300,
               fallback: bool = False, keep: bool = False) -> dict:
    """X100 侧预编译一个候选（可多 shape）。**永不抛异常**（BoardUnreachable 也吞掉），
    返回记录 dict：调用方据此决定是否仍提交作业（FAIL 不阻塞，走 A100 冷编译兜底）。

    记录字段：ok / path（driver|bench_op_fallback|fail|unreachable）/ op / shapes /
    dtype / candidate_sha256 / total_s / reason / cpus_allowed / cache_*_delta / detail
    """
    t0 = time.perf_counter()
    cp = Path(candidate)
    base = {"op": op, "shapes": list(shapes), "dtype": dtype, "board": board,
            "candidate": str(cp), "candidate_sha256": None}
    try:
        base["candidate_sha256"] = hashlib.sha256(cp.read_bytes()).hexdigest()
    except OSError as exc:
        return dict(base, ok=False, path="fail", reason=f"候选不可读: {exc}",
                    total_s=round(time.perf_counter() - t0, 1))

    tmp = tempfile.mkdtemp(prefix="k3_precompile_")
    remote_cand = f"{TDIR}/{cp.name}"
    try:
        # driver + 候选落到本地临时目录（上传用相对名，避开 pssh --put 的冒号切分）
        (Path(tmp) / DRIVER_NAME).write_text(DRIVER_SRC, encoding="utf-8")
        (Path(tmp) / cp.name).write_bytes(cp.read_bytes())
        _run_pssh([board, f"mkdir -p {TDIR}"], 60)
        _put(board, DRIVER_NAME, f"{TDIR}/{DRIVER_NAME}", tmp)
        _put(board, cp.name, remote_cand, tmp)
        cmd = (f"source {RUN_ENV} >/dev/null 2>&1; timeout {int(timeout_s)} "
               f"python {TDIR}/{DRIVER_NAME} '{remote_cand}' '{op}' '{dtype}' "
               f"'{json.dumps(list(shapes))}'")
        rc, out = _run_pssh([board, cmd, "--timeout", str(int(timeout_s))], int(timeout_s))
        verdict, detail = _parse_contract(out)
        rec = dict(base, total_s=round(time.perf_counter() - t0, 1),
                   ok=verdict == "OK", path="driver" if verdict == "OK" else "fail",
                   reason=None if verdict == "OK" else (out.strip()[-400:] or f"rc={rc}"),
                   cpus_allowed=(detail or {}).get("cpus_allowed"),
                   cache_entries_delta=(detail or {}).get("cache_entries_delta"),
                   cache_files_delta=(detail or {}).get("cache_files_delta"),
                   detail=detail)
        if verdict != "OK" and fallback:
            fb = _fallback_bench_op(board, op, shapes, dtype, timeout_s)
            rec.update(fb)
        return rec
    except BoardUnreachable as exc:
        return dict(base, ok=False, path="unreachable", reason=str(exc)[:400],
                    total_s=round(time.perf_counter() - t0, 1))
    except Exception as exc:  # noqa: BLE001 任何意外都不阻塞主流程
        return dict(base, ok=False, path="fail", reason=f"{type(exc).__name__}: {exc}"[:400],
                    total_s=round(time.perf_counter() - t0, 1))
    finally:
        if not keep:
            try:
                _run_pssh([board, f"rm -f '{remote_cand}'"], 30)
            except BoardUnreachable:
                pass
        for name in (DRIVER_NAME, cp.name):
            try:
                os.unlink(os.path.join(tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def _fallback_bench_op(board: str, op: str, shapes: list[str], dtype: str,
                       timeout_s: int) -> dict:
    """bench_op --candidate-dir 回退（**持计时锁**，会堵 timing 车道）。

    默认关闭；只在显式 --fallback 时走。逐 shape 触发编译（warmup 0/rep 1，
    不计性能），成功即算 OK。

    多计时车道部署下（ADR-0014）必须**同时持有全部分区锁**，否则会与某条车道
    重叠；连 base 锁一起持也兼容单车道部署（flock 会创建不存在的锁文件）。
    """
    ok_shapes, errs = [], []
    t0 = time.perf_counter()
    locks = " ".join(f"flock {LOCK_BASE}{sfx}" for sfx in ("", ".0", ".1"))
    for sh in shapes:
        cmd = (f"source {RUN_ENV} >/dev/null 2>&1; export PYTHONPATH={TDIR}:${{PYTHONPATH:-}}; "
               f"timeout {int(timeout_s)} {locks} python {BENCH_OP} "
               f"--op {op} --shape {sh} --dtype {dtype} --warmup 0 --rep 1 --candidate-dir {TDIR}")
        try:
            rc, out = _run_pssh([board, cmd, "--timeout", str(int(timeout_s))], int(timeout_s))
        except BoardUnreachable as exc:
            return {"ok": False, "path": "unreachable", "reason": str(exc)[:400],
                    "total_s": round(time.perf_counter() - t0, 1)}
        if rc == 0 and re.search(r"^\{", out, re.M):
            ok_shapes.append(sh)
        else:
            errs.append(f"{sh}: rc={rc} {out.strip()[-200:]}")
    if not ok_shapes:
        return {"ok": False, "path": "fail", "reason": "bench_op 回退亦失败: " + "; ".join(errs)[:400],
                "total_s": round(time.perf_counter() - t0, 1)}
    return {"ok": True, "path": "bench_op_fallback", "reason": None,
            "fallback_shapes_ok": ok_shapes, "fallback_errors": errs[:4],
            "total_s": round(time.perf_counter() - t0, 1)}


def _validate(candidate: str, op: str, shapes: list[str], dtype: str) -> str | None:
    """注入面收口：所有值进板上命令串前过白名单（与 evald/sh 版同风格）。"""
    if not RE_OP.match(op):
        return f"op 不合法: {op}"
    if dtype not in DTYPES:
        return f"dtype 不支持: {dtype}"
    if not shapes or len(shapes) > MAX_SHAPES:
        return f"shape 数须为 1..{MAX_SHAPES}: {shapes}"
    for s in shapes:
        if not RE_SHAPE.match(s):
            return f"shape 不合法（如 64,512,512）: {s!r}"
    if len(set(shapes)) != len(shapes):
        return "shape 列表内不可重复"
    if not os.path.isfile(candidate):
        return f"候选文件不存在: {candidate}"
    if not RE_CAND.match(os.path.basename(candidate)):
        return f"候选文件名须为 [A-Za-z0-9._-]+.py: {candidate}"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="X100 预编译（pssh 通道）")
    ap.add_argument("candidate", help="候选文件本地路径")
    ap.add_argument("op")
    ap.add_argument("shapes", help="逗号分隔单 shape，或分号分隔多 shape（如 64,512,512;1024,1024）")
    ap.add_argument("dtype", choices=sorted(DTYPES))
    ap.add_argument("--board", default=DEFAULT_BOARD)
    ap.add_argument("--timeout", type=int, default=300, help="板上编译超时（秒）")
    ap.add_argument("--fallback", action="store_true",
                    help="driver 失败时回退 bench_op（**持计时锁**，默认关）")
    ap.add_argument("--keep", action="store_true", help="保留板上候选文件")
    args = ap.parse_args()

    shapes = [s for s in args.shapes.split(";") if s]
    err = _validate(args.candidate, args.op, shapes, args.dtype)
    if err:
        print(f"FATAL: {err}", file=sys.stderr)
        return 2

    rec = precompile(args.candidate, args.op, shapes, args.dtype, board=args.board,
                     timeout_s=args.timeout, fallback=args.fallback, keep=args.keep)
    if rec.get("detail"):
        print("PRECOMPILE_JSON " + json.dumps(rec["detail"], ensure_ascii=False))
    if rec["path"] == "unreachable":
        print(f"FATAL: 板不可达: {rec.get('reason')}", file=sys.stderr)
        return 4
    if rec["ok"]:
        print(f"[precompile] OK {args.op} path={rec['path']} total_s={rec['total_s']} "
              f"cpus={rec.get('cpus_allowed')} cache_entries_delta={rec.get('cache_entries_delta')}")
        return 0
    print(f"PRECOMPILE_FAIL {args.op} {rec.get('reason')}")
    print(f"[precompile] FAIL（不阻塞提交，作业将走 A100 冷编译）total_s={rec['total_s']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
