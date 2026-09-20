#!/usr/bin/env python
"""单进程 torch 参照表（grp-TORCH-T2 / glm5.3f_残留实验 §四.2，规程＝handoff-torch-arm-externalization §4）。

一个进程内 import torch/flag_gems 各一次，循环全部 (op, shape, dtype)：
  直调可行 -> 3 遍 triton.testing.do_bench(ref_fn, warmup=5, rep=5, median)
  直调不可行 -> 复用 bench_op._framework_bench（FlagGems benchmark 机制，n=1 取机制内 median）
复用板上 bench_op 的输入构造与分发表（_make_input/_SPECIAL_SHAPE/_SPECIAL/BINARY/UNARY/…），
gems 侧闭包保留但从不调用；直调分发只探测 torch 侧（等价性：bench_op main 的 ref_fn() 探测
失败同样落框架兜底，故 ref_fn 可行性 == torch 侧探测可行性）。

输出 JSONL（逐 op 追加 + flush，支持断点续跑：重启跳过已 done 的 op）：
  首行 {"_config_tag": {...}}（仅新文件写）
  每 op: {op, shape, dtype, state, framework, torch_ms_median, torch_ms_all, n, wall_ms, error?}

板上用法（静默窗 + 全锁外层由 run_T2.sh 负责）:
  source ~/autoke/scripts/run_env.sh && python torch_reference.py --out /tmp/torch_ref/T2.jsonl
"""

import argparse
import gc
import hashlib
import importlib.util
import json
import os
import signal
import statistics
import sys
import time
import traceback
from datetime import datetime, timezone

SCRIPTS_ROOT = "/home/c4y/autoke/scripts"
BENCH_OP_PATH = os.path.join(SCRIPTS_ROOT, "bench_op.py")
EVALD_PATHS = ["/home/c4y/autoke/evald/evald.py", "/home/c4y/autoke/evald/warmd.py"]
DEFAULT_SHAPE = "64,512,512"

# 230 op 全集（results/e1-ops/baseline_manifest.json，2026-09-10 快照）；
# 元组 = (op, shape, dtype)；shape=="framework" 表示框架族标记（直调仍以 DEFAULT_SHAPE 尝试，
# 与 09-07 evald 基线作业 make_job 口径一致）
OPS = [
    ('abs', '64,512,512', 'float32'),
    ('absolute', '64,512,512', 'float32'),
    ('acosh', '64,512,512', 'float32'),
    ('add', '64,512,512', 'float32'),
    ('addmv', '512,512', 'float32'),
    ('addr', '512,512', 'float32'),
    ('alias_copy', '64,512,512', 'float32'),
    ('allclose', '64,512,512', 'float32'),
    ('angle', '64,512,512', 'float32'),
    ('arange', '64,512,512', 'float32'),
    ('arcsinh', '64,512,512', 'float32'),
    ('arctan2', '64,512,512', 'float32'),
    ('argmax', '64,512,512', 'float32'),
    ('argmin', '64,512,512', 'float32'),
    ('asinh', '64,512,512', 'float32'),
    ('atan2', '64,512,512', 'float32'),
    ('atanh', '64,512,512', 'float32'),
    ('bernoulli', '64,512,512', 'float32'),
    ('bitwise_left_shift', '64,512,512', 'int32'),
    ('bitwise_not', '64,512,512', 'int32'),
    ('bitwise_right_shift', '64,512,512', 'int32'),
    ('broadcast_tensors', '64,512,512', 'float32'),
    ('ceil', '64,512,512', 'float32'),
    ('celu', '64,512,512', 'float32'),
    ('clip', '64,512,512', 'float32'),
    ('conj_physical', '64,512,512', 'float32'),
    ('copysign', '64,512,512', 'float32'),
    ('cos', '64,512,512', 'float32'),
    ('cosh', '64,512,512', 'float32'),
    ('deg2rad', '64,512,512', 'float32'),
    ('diag_embed', '128,128', 'float32'),
    ('diagonal_copy', '64,512,512', 'float32'),
    ('digamma', '64,512,512', 'float32'),
    ('div', '64,512,512', 'float32'),
    ('dot', '4096', 'float32'),
    ('eq', '64,512,512', 'float32'),
    ('equal', '64,512,512', 'float32'),
    ('erf', '64,512,512', 'float32'),
    ('exp', '64,512,512', 'float32'),
    ('exp2', '64,512,512', 'float32'),
    ('expm1', '64,512,512', 'float32'),
    ('flatten', '64,512,512', 'float32'),
    ('floor', '64,512,512', 'float32'),
    ('floor_divide', '64,512,512', 'float32'),
    ('fmax', '64,512,512', 'float32'),
    ('fmin', '64,512,512', 'float32'),
    ('frac', '64,512,512', 'float32'),
    ('ge', '64,512,512', 'float32'),
    ('gelu', '64,512,512', 'float32'),
    ('greater', '64,512,512', 'float32'),
    ('gt', '64,512,512', 'float32'),
    ('hypot', '64,512,512', 'float32'),
    ('i0', '64,512,512', 'float32'),
    ('isclose', '64,512,512', 'float32'),
    ('isfinite', '64,512,512', 'float32'),
    ('isinf', '64,512,512', 'float32'),
    ('isnan', '64,512,512', 'float32'),
    ('isneginf', '64,512,512', 'float32'),
    ('isposinf', '64,512,512', 'float32'),
    ('kron', '16,16', 'float32'),
    ('layer_norm', '64,512,512', 'float32'),
    ('le', '64,512,512', 'float32'),
    ('less_equal', '64,512,512', 'float32'),
    ('log', '64,512,512', 'float32'),
    ('log10', '64,512,512', 'float32'),
    ('log1p', '64,512,512', 'float32'),
    ('log2', '64,512,512', 'float32'),
    ('log_softmax', '64,512,512', 'float32'),
    ('logaddexp', '64,512,512', 'float32'),
    ('logaddexp2', '64,512,512', 'float32'),
    ('logical_and', '64,512,512', 'bool'),
    ('logical_not', '64,512,512', 'bool'),
    ('logical_or', '64,512,512', 'bool'),
    ('logical_xor', '64,512,512', 'bool'),
    ('logit', '64,512,512', 'float32'),
    ('lt', '64,512,512', 'float32'),
    ('maximum', '64,512,512', 'float32'),
    ('median', '1024', 'float32'),
    ('minimum', '64,512,512', 'float32'),
    ('mul', '64,512,512', 'float32'),
    ('mv', '512,512', 'float32'),
    ('ne', '64,512,512', 'float32'),
    ('neg', '64,512,512', 'float32'),
    ('negative', '64,512,512', 'float32'),
    ('not_equal', '64,512,512', 'float32'),
    ('ones_like', '64,512,512', 'float32'),
    ('outer', '4096', 'float32'),
    ('pdist', '128,64', 'float32'),
    ('rad2deg', '64,512,512', 'float32'),
    ('rand_like', '64,512,512', 'float32'),
    ('reciprocal', '64,512,512', 'float32'),
    ('relu', '64,512,512', 'float32'),
    ('remainder', '64,512,512', 'float32'),
    ('rms_norm', '64,512,512', 'float32'),
    ('rot90', '64,512,512', 'float32'),
    ('round', '64,512,512', 'float32'),
    ('rsqrt', '64,512,512', 'float32'),
    ('selu', '64,512,512', 'float32'),
    ('sgn', '64,512,512', 'float32'),
    ('sigmoid', '64,512,512', 'float32'),
    ('sign', '64,512,512', 'float32'),
    ('signbit', '64,512,512', 'float32'),
    ('silu', '64,512,512', 'float32'),
    ('sin', '64,512,512', 'float32'),
    ('sinc', '64,512,512', 'float32'),
    ('sinh', '64,512,512', 'float32'),
    ('slice_scatter', '64,512,512', 'float32'),
    ('sqrt', '64,512,512', 'float32'),
    ('square', '64,512,512', 'float32'),
    ('squeeze_copy', '64,512,512', 'float32'),
    ('sub', '64,512,512', 'float32'),
    ('t_copy', '512,512', 'float32'),
    ('tanh', '64,512,512', 'float32'),
    ('trace', '512,512', 'float32'),
    ('true_divide', '64,512,512', 'float32'),
    ('unbind', '64,512,512', 'float32'),
    ('unbind_copy', '64,512,512', 'float32'),
    ('zeros_like', '64,512,512', 'float32'),
    ('logspace', 'framework', 'float32'),  # fw
    ('threshold', 'framework', 'float32'),  # fw
    ('flip', 'framework', 'float32'),  # fw
    ('dropout', 'framework', 'float32'),  # fw
    ('functional_sym_constrain_range', 'framework', 'float32'),  # fw
    ('select_scatter', 'framework', 'float32'),  # fw
    ('full', 'framework', 'float32'),  # fw
    ('add_relu', 'framework', 'float32'),  # fw
    ('full_like', 'framework', 'float32'),  # fw
    ('bitwise_and', 'framework', 'int32'),  # fw
    ('prelu_kernel', 'framework', 'float32'),  # fw
    ('linspace', 'framework', 'float32'),  # fw
    ('greater_equal', 'framework', 'float32'),  # fw
    ('less', 'framework', 'float32'),  # fw
    ('safe_softmax', 'framework', 'float32'),  # fw
    ('sparse_semi_structured_mm', 'framework', 'float32'),  # fw
    ('tile', 'framework', 'float32'),  # fw
    ('index_copy', 'framework', 'float32'),  # fw
    ('unsafe_masked_index', 'framework', 'float32'),  # fw
    ('jagged_to_padded_dense_forward', 'framework', 'float32'),  # fw
    ('kthvalue', 'framework', 'float32'),  # fw
    ('margin_ranking_loss', 'framework', 'float32'),  # fw
    ('resize_output', 'framework', 'float32'),  # fw
    ('group_norm', 'framework', 'float32'),  # fw
    ('fused_adam', 'framework', 'float32'),  # fw
    ('batch_norm_no_update', 'framework', 'float32'),  # fw
    ('permute_copy', 'framework', 'float32'),  # fw
    ('split_with_sizes_copy', 'framework', 'float32'),  # fw
    ('pixel_shuffle', 'framework', 'float32'),  # fw
    ('as_strided_copy', 'framework', 'float32'),  # fw
    ('renorm', 'framework', 'float32'),  # fw
    ('channel_shuffle', 'framework', 'float32'),  # fw
    ('chunk_cat', 'framework', 'float32'),  # fw
    ('tensor_split', 'framework', 'float32'),  # fw
    ('chunk', 'framework', 'float32'),  # fw
    ('unfold_copy', 'framework', 'float32'),  # fw
    ('transpose', 'framework', 'float32'),  # fw
    ('unsafe_view', 'framework', 'float32'),  # fw
    ('make_dep_token', 'framework', 'float32'),  # fw
    ('narrow', 'framework', 'float32'),  # fw
    ('unfold_view', 'framework', 'float32'),  # fw
    ('unsqueeze', 'framework', 'float32'),  # fw
    ('assert_async', 'framework', 'float32'),  # fw
    ('_reshape_alias', 'framework', 'float32'),  # fw
    ('affine_grid_generator', 'framework', 'float32'),  # fw
    ('alias', 'framework', 'float32'),  # fw
    ('apply_rotary_pos_emb', 'framework', 'float32'),  # fw
    ('cdist_forward', 'framework', 'float32'),  # fw
    ('col2im', 'framework', 'float32'),  # fw
    ('constant_pad_nd', 'framework', 'float32'),  # fw
    ('contiguous', 'framework', 'float32'),  # fw
    ('diagonal_scatter', 'framework', 'float32'),  # fw
    ('elu', 'framework', 'float32'),  # fw
    ('embedding', 'framework', 'float32'),  # fw
    ('empty', 'framework', 'float32'),  # fw
    ('euclidean_dist', 'framework', 'float32'),  # fw
    ('expand', 'framework', 'float32'),  # fw
    ('expand_as', 'framework', 'float32'),  # fw
    ('functional_sym_constrain_range_for_size', 'framework', 'float32'),  # fw
    ('gather_block_quantized', 'framework', 'float32'),  # fw
    ('glu', 'framework', 'float32'),  # fw
    ('hardsigmoid', 'framework', 'float32'),  # fw
    ('ilshift', 'framework', 'int32'),  # fw
    ('is_all_true', 'framework', 'bool'),  # fw
    ('is_nonzero', 'framework', 'float32'),  # fw
    ('leaky_relu', 'framework', 'float32'),  # fw
    ('lift', 'framework', 'float32'),  # fw
    ('lift_fresh_copy', 'framework', 'float32'),  # fw
    ('log_sigmoid', 'framework', 'float32'),  # fw
    ('log_sigmoid_forward', 'framework', 'float32'),  # fw
    ('lshift', 'framework', 'int32'),  # fw
    ('max_unpool2d', 'framework', 'float32'),  # fw
    ('max_unpool3d', 'framework', 'float32'),  # fw
    ('mish', 'framework', 'float32'),  # fw
    ('new_ones', 'framework', 'float32'),  # fw
    ('norm', 'framework', 'float32'),  # fw
    ('ones', 'framework', 'float32'),  # fw
    ('pad', 'framework', 'float32'),  # fw
    ('pixel_unshuffle', 'framework', 'float32'),  # fw
    ('prelu', 'framework', 'float32'),  # fw
    ('rand', 'framework', 'float32'),  # fw
    ('randn', 'framework', 'float32'),  # fw
    ('relu6', 'framework', 'float32'),  # fw
    ('repeat', 'framework', 'float32'),  # fw
    ('resize', 'framework', 'float32'),  # fw
    ('resize_as', 'framework', 'float32'),  # fw
    ('rrelu_with_noise_functional', 'framework', 'float32'),  # fw
    ('rwkv_ka_fusion', 'framework', 'float32'),  # fw
    ('rwkv_mm_sparsity', 'framework', 'float32'),  # fw
    ('scalar_tensor', 'framework', 'float32'),  # fw
    ('smooth_l1_loss', 'framework', 'float32'),  # fw
    ('softplus', 'framework', 'float32'),  # fw
    ('softshrink', 'framework', 'float32'),  # fw
    ('special_airy_ai', 'framework', 'float32'),  # fw
    ('special_chebyshev_polynomial_u', 'framework', 'float32'),  # fw
    ('special_digamma', 'framework', 'float32'),  # fw
    ('special_erfc', 'framework', 'float32'),  # fw
    ('special_erfinv', 'framework', 'float32'),  # fw
    ('special_i0e', 'framework', 'float32'),  # fw
    ('special_i1', 'framework', 'float32'),  # fw
    ('special_legendre_polynomial_p', 'framework', 'float32'),  # fw
    ('special_log1p', 'framework', 'float32'),  # fw
    ('special_logsumexp', 'framework', 'float32'),  # fw
    ('special_modified_bessel_k1', 'framework', 'float32'),  # fw
    ('special_scaled_modified_bessel_k1', 'framework', 'float32'),  # fw
    ('special_shifted_chebyshev_polynomial_v', 'framework', 'float32'),  # fw
    ('special_sinc', 'framework', 'float32'),  # fw
    ('sym_stride', 'framework', 'float32'),  # fw
    ('vector_norm', 'framework', 'float32'),  # fw
    ('view_copy', 'framework', 'float32'),  # fw
    ('zero', 'framework', 'float32'),  # fw
    ('zeros', 'framework', 'float32'),  # fw
]


class OpTimeout(Exception):
    pass


def _on_alarm(sig, frm):
    raise OpTimeout()


def await_subprocess(cmd, timeout_s):
    """跑一条板上命令（生产同参 bench_op），超时杀进程组。返回 {rc, out, err, state, error}。"""
    import subprocess as sp
    try:
        p = sp.Popen(["bash", "-c", cmd], stdout=sp.PIPE, stderr=sp.PIPE,
                     text=True, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        return {"rc": -1, "out": "", "err": "", "state": "failed",
                "error": f"Popen {type(e).__name__}: {e}"}
    try:
        out, err = p.communicate(timeout=timeout_s)
        return {"rc": p.returncode, "out": out or "", "err": err or "",
                "state": "failed" if p.returncode != 0 else "done", "error": None}
    except sp.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        out, err = p.communicate()
        return {"rc": -9, "out": out or "", "err": err or "",
                "state": "timeout", "error": f"subprocess timeout {timeout_s}s"}


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_bench_op():
    spec = importlib.util.spec_from_file_location("bench_op_ref", BENCH_OP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_op_ref"] = mod
    spec.loader.exec_module(mod)  # 内部 import torch/triton/flag_gems（run_env 已就绪）
    return mod


def torch_dispatch(B, op, a, b):
    """bench_op.make_fn 的 torch-only 镜像：返回 ref_fn 或 None（None→框架兜底）。"""
    t = B.torch
    if op in B.BINARY:
        return (lambda: getattr(t, op)(a, b))
    if op in B.UNARY:
        tfn = B._TORCH_FN.get(op, lambda x: getattr(t, op)(x))
        return (lambda: tfn(a))
    if op in B.REDUCE:
        return (lambda: getattr(t, op)(a))
    if op in B.ARG:
        return (lambda: getattr(t, op)(a))
    if op in B.DIM_OPS:
        return (lambda: getattr(t, op)(a, -1))
    if op in B.NORM_OPS:
        ns = a.shape[-1]
        if op == "layer_norm":
            return (lambda: t.layer_norm(a, (ns,)))
        return (lambda: t.nn.functional.rms_norm(a, (ns,)))
    if op in B._SPECIAL:
        torch_fn, _ = B._SPECIAL[op](a, b)
        return torch_fn
    if op in B._FORCE_FRAMEWORK:
        return None
    # 通用兜底：与 _generic_fn 同构，但 arity 试探只打 torch 侧（不触碰 gems 内核）
    fg = getattr(B.flag_gems, op, None)
    if fg is None:
        cand = getattr(__import__("flag_gems.ops", fromlist=[op]), op, None)
        if cand is not None and not callable(cand):
            cand = getattr(cand, op, None)
        fg = cand
    if fg is None or not callable(fg):
        return None
    try:
        n_req = sum(
            1 for p in __import__("inspect").signature(fg).parameters.values()
            if p.default is __import__("inspect").Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    except (TypeError, ValueError):
        n_req = 1
    t_op = getattr(t, op, None) or B._TORCH_FN.get(op)
    if t_op is None:
        return None
    if n_req <= 1:
        return (lambda: t_op(a))
    if n_req == 2:
        b2 = B._make_input(a.shape, a.dtype)
        try:
            t_op(a, b2)
            return (lambda: t_op(a, b2))
        except Exception:  # noqa: BLE001
            pass
        try:
            t_op(a, -1)
            return (lambda: t_op(a, -1))
        except Exception:  # noqa: BLE001
            return None
    if n_req == 3:
        cond = t.rand(a.shape) > 0.5
        try:
            t_op(cond, a, a)
            return (lambda: t_op(cond, a, a))
        except Exception:  # noqa: BLE001
            return None
    return None


def run_one(B, triton, op, shape_str, dtype_str, passes, direct_alarm, fw_alarm):
    B.torch.manual_seed(0)
    shape_eff = B._SPECIAL_SHAPE.get(op, shape_str if shape_str != "framework" else DEFAULT_SHAPE)
    shape = tuple(int(x) for x in shape_eff.split(","))
    dtype = B.DTYPES[dtype_str]
    a = B._make_input(shape, dtype)
    b = B._make_input(shape, dtype) if op in B.BINARY else None

    ref_fn = torch_dispatch(B, op, a, b)
    if ref_fn is not None:
        try:
            ref_fn()  # 探测（含 torch 首次内核初始化）
        except Exception:  # noqa: BLE001
            ref_fn = None

    if ref_fn is not None:
        vals = []
        for _ in range(passes):
            signal.alarm(direct_alarm)
            v = triton.testing.do_bench(ref_fn, warmup=5, rep=5, return_mode="median")
            signal.alarm(0)
            vals.append(round(float(v), 4))
        return {"state": "done", "framework": False, "shape": shape_eff,
                "torch_ms_median": round(statistics.median(vals), 4),
                "torch_ms_all": vals, "n": len(vals)}

    # 框架兜底：新起子进程跑板上 bench_op.py（生产同源同参，__main__ 路径）。
    # 不再进程内调 _framework_bench：长进程直调组跑完后累积状态会让机制确定性
    # pytest-Failed（logspace/threshold/flip 3/3 复现；新进程冒烟 2/2 通过）。
    # 子进程不重复 flock（父进程已持全锁，flock fd 由 exec 链继承）；
    # A100 注册 + taskset 8-11 照 wave1_board_run.sh 姿势在子进程重做。
    cmd = (f'echo $$ > /proc/set_ai_thread && exec taskset -c 8-11 '
           f'python {BENCH_OP_PATH} --op {op} --shape '
           f'{shape_str if shape_str != "framework" else DEFAULT_SHAPE} '
           f'--dtype {dtype_str} --warmup 5 --rep 5')
    t0 = time.monotonic()
    proc = await_subprocess(cmd, fw_alarm + 30)
    wall = time.monotonic() - t0
    if proc["rc"] != 0:
        return {"state": proc["state"], "shape": "framework",
                "error": (proc["error"] or f"rc={proc['rc']}")[:300]}
    line = proc["out"].strip().splitlines()[-1] if proc["out"].strip() else ""
    r = json.loads(line)
    return {"state": "done", "framework": True, "shape": "framework",
            "torch_ms_median": r.get("torch_ms"), "torch_ms_all": [r.get("torch_ms")],
            "n": 1, "gems_ms": r.get("gems_ms"), "speedup_job": r.get("speedup"),
            "correct": r.get("correct"), "fw_wall_s": round(wall, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--passes", type=int, default=3, help="直调路径遍数（框架兜底固定 n=1）")
    ap.add_argument("--direct-alarm", type=int, default=120, help="直调单遍秒级超时")
    ap.add_argument("--fw-alarm", type=int, default=420, help="框架兜底单 op 秒级超时")
    ap.add_argument("--deadline-min", type=float, default=70.0,
                    help="全局软截止（分钟），到点剩余 op 记 not_run 由外层续跑")
    args = ap.parse_args()

    deadline = time.monotonic() + args.deadline_min * 60
    signal.signal(signal.SIGALRM, _on_alarm)

    ops = [(op, sh, dt) for (op, sh, dt) in OPS if sh != "framework"] + \
          [(op, sh, dt) for (op, sh, dt) in OPS if sh == "framework"]
    smoke = os.environ.get("T2_SMOKE")
    if smoke:  # 冒烟：前 N 个直调 op ＋ 前两个框架族 op（写独立 --out，不碰正表）
        ops = ops[:int(smoke)] + [o for o in OPS if o[1] == "framework"][:2]

    done, crashed_before = set(), set()
    out_d = os.path.dirname(os.path.abspath(args.out))
    crashskip_path = os.path.join(out_d, "T2.crashskip")
    current_path = os.path.join(out_d, "T2.current")
    fresh = not os.path.exists(args.out)
    os.makedirs(out_d, exist_ok=True)
    if not fresh:
        with open(args.out, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("state") == "done":
                        done.add(r.get("op"))
                except json.JSONDecodeError:
                    pass
        # 上次进程死于 marker 记录的 op（未落入 done）→ 记入 crashskip（进程级死亡
        # 如 SIGABRT/OutcomeException 逃逸，同 op 重试必再死，跳过换进度）
        try:
            last = open(current_path, encoding="utf-8").read().strip()
        except OSError:
            last = ""
        if last and last not in done:
            crashed_before.add(last)
            with open(crashskip_path, "a", encoding="utf-8") as f:
                f.write(last + "\n")
    crashskip = set()
    if os.path.exists(crashskip_path):
        for line in open(crashskip_path, encoding="utf-8"):
            if line.strip():
                crashskip.add(line.strip())
    todo = [(op, sh, dt) for (op, sh, dt) in ops
            if op not in done and op not in crashskip]
    for op in sorted(crashskip - done - {o[0] for o in todo}):
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"op": op, "state": "crashed",
                                "error": "process died on prior attempt; skip-listed"},
                               ensure_ascii=False) + "\n")

    if fresh:
        tag = {
            "cluster": "A100", "cpus": "8-11", "omp_threads": 4, "lanes": 0,
            "warmup": 5, "rep": 5, "direct_passes": args.passes, "fw_passes": 1,
            "board": "SpacemitK3", "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "bench_op_md5": _md5(BENCH_OP_PATH),
            "run_env_md5": _md5(os.path.join(SCRIPTS_ROOT, "run_env.sh")),
        }
        for p in EVALD_PATHS:
            if os.path.exists(p):
                tag[os.path.basename(p) + "_md5"] = _md5(p)
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps({"_config_tag": tag}, ensure_ascii=False) + "\n")
            f.flush()

    import triton  # noqa: E402  与 bench_op 同 venv（run_env 已 source）
    B = load_bench_op()

    print(f"[T2] total={len(ops)} done_skip={len(done)} todo={len(todo)} "
          f"deadline_min={args.deadline_min}", flush=True)
    n_done = n_fail = 0
    t_all = time.monotonic()
    for i, (op, shape_str, dtype_str) in enumerate(todo):
        if time.monotonic() > deadline:
            with open(args.out, "a", encoding="utf-8") as f:
                f.write(json.dumps({"op": op, "state": "not_run",
                                    "note": "deadline"}, ensure_ascii=False) + "\n")
            continue
        rec = {"op": op, "shape": shape_str, "dtype": dtype_str,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with open(current_path, "w", encoding="utf-8") as f:
            f.write(op)  # 进程级死亡标记：重启时据此把该 op 记入 crashskip
        t0 = time.monotonic()
        try:
            rec.update(run_one(B, triton, op, shape_str, dtype_str,
                               args.passes, args.direct_alarm, args.fw_alarm))
            n_done += 1
        except OpTimeout:
            rec.update({"state": "timeout", "error": "alarm"})
            n_fail += 1
        except SystemExit as e:
            rec.update({"state": "failed", "error": f"SystemExit: {e}"[:300]})
            n_fail += 1
        except BaseException as e:  # noqa: BLE001  pytest OutcomeException 系
            # （_pytest.outcomes.Failed/Skipped 继承 BaseException 而非 Exception，
            #   逃逸会杀进程——必须在这里收口记 failed 继续）
            tb = traceback.format_exc(limit=3)
            rec.update({"state": "failed",
                        "error": f"{type(e).__name__}: {e}"[:200],
                        "trace_tail": tb[-400:]})
            n_fail += 1
        rec["wall_ms"] = int((time.monotonic() - t0) * 1000)
        with open(args.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        print(f"[T2 {i + 1}/{len(todo)}] {op}: {rec['state']} "
              f"ms={rec.get('torch_ms_median')} fw={rec.get('framework')} "
              f"wall={rec['wall_ms']}ms", flush=True)
        try:
            del rec
        except NameError:
            pass
        gc.collect()
    print(f"[T2] ALL: done={n_done} fail={n_fail} "
          f"total_min={round((time.monotonic() - t_all) / 60, 1)}", flush=True)


if __name__ == "__main__":
    main()
