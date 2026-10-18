#!/usr/bin/env python
"""单算子单 shape 快速微基准：torch 参考延迟 vs flag_gems 延迟 + 精度参考。

相比 pytest 全量 benchmark（~2 分钟/算子），本脚本 5~30 秒出结果，
供优化迭代循环用：改 op 源码 -> 跑本脚本 -> 看 speedup/correct。

用法（须先 source ~/autoke/scripts/run_env.sh）::

  python ~/autoke/scripts/bench_op.py --op add --shape 64,512,512 --dtype float32
  python ~/autoke/scripts/bench_op.py --op sum --shape 4194304 --dtype float16 --warmup 50 --rep 50

输出单行 JSON：speedup = torch_ms / gems_ms；correct 为宽松一致性参考
（fp16: rtol/atol=5e-2；fp32: 5e-3；argmax 精确相等），严格验证用 FlagGems tests。
"""

import argparse
import json
import os
import re
import sys

import torch
import torch.nn.functional as F
import triton


# --candidate-dir <目录>（precompile_candidate.sh 回退路径用）：把目录下 <op>.py
# （或唯一 *<op>*.py）以 flag_gems.ops.<op> 的模块身份加载——与 evald 部署同一
# import 语义（候选在 flag_gems 初始化期被加载，kernel 编译写共享 triton 缓存）。
# 钩子必须先于 import flag_gems 安装，故此处提前从 argv 抽参（正式 argparse 在 main）。
def _install_candidate_loader(argv=None):
    argv = sys.argv if argv is None else argv
    if "--candidate-dir" not in argv:
        return None

    def _early_opt(name):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        for a in argv:
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return None

    op, cand_dir = _early_opt("--op"), _early_opt("--candidate-dir")
    if not op or not cand_dir:
        sys.stderr.write("[bench_op] --candidate-dir 需要 --op 与目录参数\n")
        raise SystemExit(1)
    import importlib.abc
    import importlib.util

    target = f"flag_gems.ops.{op}"

    class _CandidateFinder(importlib.abc.MetaPathFinder):
        loaded = False

        def find_spec(self, fullname, path=None, target=None):
            if fullname != target:
                return None
            d = os.path.abspath(cand_dir)
            exact = os.path.join(d, f"{op}.py")
            if os.path.isfile(exact):
                f = exact
            else:
                hits = sorted(x for x in os.listdir(d)
                              if x.endswith(".py") and op in x) \
                    if os.path.isdir(d) else []
                if len(hits) != 1:
                    raise ImportError(
                        f"--candidate-dir {d} 下未找到唯一 *{op}*.py（命中 {len(hits)}: {hits[:5]}）")
                f = os.path.join(d, hits[0])
            _CandidateFinder.loaded = True
            sys.stderr.write(f"[bench_op] candidate-dir: {fullname} <- {f}\n")
            return importlib.util.spec_from_file_location(fullname, f)

    sys.meta_path.insert(0, _CandidateFinder())
    return _CandidateFinder


_CANDIDATE_FINDER = _install_candidate_loader()

import flag_gems  # noqa: E402

BINARY = {"add", "sub", "mul", "div", "div_tensor"}
# torch.silu 在该环境报 AttributeError（疑似被 patch），统一走 F.* 形式
_TORCH_FN = {"silu": F.silu, "gelu": F.gelu, "sigmoid": F.sigmoid, "tanh": F.tanh}
# 通用兜底：未知算子按 flag_gems 顶层签名推断 arity（1/2/3 元）
import inspect


def _die(msg: str):
    """报错退出。warmd 的 SystemExit 处理器不打印 message，必须自己写 stderr。"""
    sys.stderr.write(f"[bench_op] {msg}\n")
    sys.stderr.flush()
    raise SystemExit(1)


# 高频特判表：第二必选参数不是 tensor 的算子（arity 推断无法覆盖）。
# 每项 fn(a, b) -> (torch_lambda, flag_gems_lambda)；直调失败仍会落框架兜底。
def _vec(t):
    return torch.randn(t.shape[-1], dtype=t.dtype)


def _sp_mv(a, b):
    v = _vec(a)
    return (lambda: torch.mv(a, v)), (lambda: flag_gems.mv(a, v))


def _sp_addmv(a, b):
    v = _vec(a)
    i = torch.randn(a.shape[0], dtype=a.dtype)
    return ((lambda: torch.addmv(i, a, v)),
            (lambda: flag_gems.addmv(i, a, v)))


def _sp_addr(a, b):
    # torch.addr(input, vec1, vec2) = beta*input + alpha*vec1⊗vec2；重活是外积
    v1 = a[0].contiguous()
    v2 = a[-1].contiguous()
    bias = torch.randn(a.shape, dtype=a.dtype)
    return ((lambda: torch.addr(bias, v1, v2)),
            (lambda: flag_gems.addr(bias, v1, v2)))


def _prob(a):
    return torch.rand(a.shape, dtype=a.dtype)


def _sp_where(a, b):
    cond = a > 0
    b2 = _make_input(a.shape, a.dtype)
    return ((lambda: torch.where(cond, a, b2)),
            (lambda: flag_gems.where_self(cond, a, b2)))


def _sp_pow(a, b):
    # 底数取 abs+0.5（负底数 x 分数指数 -> 双侧 NaN，allclose 恒 false）
    base = torch.abs(a) + 0.5
    e = torch.rand(a.shape, dtype=a.dtype) * 2.0 + 0.5
    return ((lambda: torch.pow(base, e)),
            (lambda: flag_gems.pow_tensor_tensor(base, e)))


_SPECIAL = {
    "cumsum": lambda a, b: (
        (lambda: torch.cumsum(a, 0)), (lambda: flag_gems.cumsum(a, 0))),
    "topk": lambda a, b: (
        (lambda: torch.topk(a, 5)), (lambda: flag_gems.topk(a, 5))),
    "mv": _sp_mv,
    "addmv": _sp_addmv,
    "addr": _sp_addr,
    "clip": lambda a, b: (
        (lambda: torch.clip(a, -1.0, 1.0)), (lambda: flag_gems.clip(a, -1.0, 1.0))),
    "bernoulli": lambda a, b: (
        (lambda: torch.bernoulli(_prob(a))), (lambda: flag_gems.bernoulli(_prob(a)))),
    "arange": lambda a, b: (
        (lambda: torch.arange(a.numel(), dtype=a.dtype)),
        (lambda: flag_gems.arange(a.numel(), dtype=a.dtype))),
    # spacemit7 探针转入：benchmark use_gems 链路对 where 挂起（4x120s 实证），
    # 必须直调 where_self；pow 直调 pow_tensor_tensor（benchmark 链路可用但直调口径统一）
    "where": _sp_where,
    "where_self": _sp_where,
    "pow": _sp_pow,
    # conv 族（grp-CONV-PROBE 探针转入）：a 即输入 (N,C[,L|,H,W])，k=3 同形
    # padding 卷积才是典型口径；arity=2 同 shape 退化卷积无代表性不作基线
    "conv1d": lambda a, b: _sp_conv(a, dim=1),
    "conv2d": lambda a, b: _sp_conv(a, dim=2),
    # dw2d 走直调（v2, grp-REQUEUE-CONV）：框架兜底道的 torch 参照
    # aten::_conv_depthwise2d 在 spine-torch 无 CPU kernel（r1 实证
    # NotImplementedError），只能直调 + F.conv2d(groups=C) 参照；
    # 顶层导出名是带下划线的 flag_gems._conv_depthwise2d（位置参 kernel_size）
    "conv_depthwise2d": lambda a, b: _sp_dw2d(a),
}


def _sp_dw2d(a):
    # a=(N,C,H,W) 输入；depthwise 权重 (C,1,3,3) stride=1/pad=1，
    # 参照 F.conv2d(groups=C)（probe dw-basic 验证过的参照口径）
    c = a.shape[1]
    w = torch.randn(c, 1, 3, 3, dtype=a.dtype)
    fn = flag_gems._conv_depthwise2d
    return ((lambda: F.conv2d(a, w, padding=1, groups=c)),
            (lambda: fn(a, w, [3, 3], None, 1, 1, 1)))


def _sp_conv(a, dim):
    # 权重 (2C, C, k[, k])、stride=1/pad=1：输出空间尺寸不变，F.* 与
    # flag_gems.conv1d/conv2d 同参直调；权重在 make_fn 期一次性构造，两侧共用
    c = a.shape[1]
    w = torch.randn(*((2 * c, c, 3) if dim == 1 else (2 * c, c, 3, 3)),
                    dtype=a.dtype)
    if dim == 1:
        return ((lambda: F.conv1d(a, w, padding=1)),
                (lambda: flag_gems.conv1d(a, w, padding=1)))
    return ((lambda: F.conv2d(a, w, padding=1)),
            (lambda: flag_gems.conv2d(a, w, padding=1)))


# 直调层的输入 shape 覆盖：算子对维度有硬约束（1D/2D/小 shape），
# 用小代表 shape 构造 a/b，避免 64,512,512 直接炸（OOM/维度断言）。
_SPECIAL_SHAPE = {
    "dot": "4096", "outer": "4096", "trace": "512,512", "t_copy": "512,512",
    "is_nonzero": "8", "median": "1024", "pdist": "128,64", "kron": "16,16",
    "mv": "512,512", "addmv": "512,512", "addr": "512,512",
    "diag_embed": "128,128",
}

# 强制走框架兜底的算子：直调探针会触发后端崩溃（spineTcmAcquire SIGABRT），
# 跳过直调层给框架层（不同构造路径）一次机会
_FORCE_FRAMEWORK = {"addr"}

# 随机性算子：输出不可比，correct 置 None
_NO_CORRECT = {"bernoulli", "rand", "randn", "rrelu_with_noise_functional", "dropout"}

# 框架兜底层按 op 覆盖 shape（写入临时 yaml，以 op_name 为 key 优先命中）。
# 用于 test 文件对维度有解包要求的算子（如 embedding 需 2D [num_emb, dim]）。
_FW_SHAPE_OVERRIDE = {
    "embedding": "[[128, 64]]",
    "kron": "[[16, 16]]",
    "pdist": "[[128, 64]]",
    "dot": "[[4096]]",
    "outer": "[[4096]]",
    "median": "[[65536]]",
    # dw2d（grp-CONV-PROBE）：test_conv_depthwise2d 无 fixture、_input_fn 自带
    # 全部构造，需 9 元组（batch,ch,h,w,kh,kw,stride,padding,dilation），否则
    # 解包 expected 9, got 3
    "conv_depthwise2d": "[[1, 8, 32, 32, 8, 3, 1, 1, 1]]",
    # conv1d（grp-REQUEUE-CONV）：test_conv1d.conv1d_input_fn 解包 8 元组
    # (batch,c_in,L,out_c,kernel,stride,padding,groups)，probe 小形同类
    "conv1d": "[[1, 3, 64, 4, 3, 1, 1, 1]]",
}


def _generic_fn(op, a, b):
    if op in _SPECIAL:
        return _SPECIAL[op](a, b)
    if op in _FORCE_FRAMEWORK:
        return None, None
    fg = getattr(flag_gems, op, None)
    if fg is None:
        cand = getattr(__import__("flag_gems.ops", fromlist=[op]), op, None)
        # ops 命名空间下可能是子模块（ops/where.py），取模块内同名函数
        if cand is not None and not callable(cand):
            cand = getattr(cand, op, None)
        fg = cand
    if fg is None or not callable(fg):
        return None, None  # 交给框架兜底
    try:
        n_req = sum(
            1 for p in inspect.signature(fg).parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        )
    except (TypeError, ValueError):
        n_req = 1
    t_op = getattr(torch, op, None) or _TORCH_FN.get(op)
    if t_op is None:
        # torch 顶层无同名函数（alias/special_*/elu 等）：直调无法对比，
        # 交框架兜底（benchmark test 里带正确 torch_op）
        return None, None
    if n_req <= 1:
        return (lambda: t_op(a)), (lambda: fg(a))
    if n_req == 2:
        # arity=2：先试探 tensor×tensor，再试探 (a, -1)（归约类 dim 参数），
        # 都失败交框架兜底
        b2 = _make_input(a.shape, a.dtype)
        try:
            fg(a, b2)
            return (lambda: t_op(a, b2)), (lambda: fg(a, b2))
        except Exception:  # noqa: BLE001
            pass
        try:
            fg(a, -1)
            return (lambda: t_op(a, -1)), (lambda: fg(a, -1))
        except Exception:  # noqa: BLE001
            return None, None
    if n_req == 3:  # where 型：cond, a, b（试探，失败交框架）
        cond = torch.rand(a.shape) > 0.5
        try:
            fg(cond, a, a)
            return (lambda: t_op(cond, a, a)), (lambda: fg(cond, a, a))
        except Exception:  # noqa: BLE001
            return None, None
    return None, None  # 更多必选参数，交框架兜底


# 框架兜底：bench_op 分发不了的长尾算子，借 FlagGems benchmark 体系构造输入与签名，
# 真实 do_bench 计时（get_latency 包装记录 torch/gems 成对延迟）。
FRAMEWORK_SHAPE_FILE = "/home/c4y/autoke/scripts/baseline_shapes.yaml"


def _framework_bench(op, dtype_str, warmup, rep):
    sys.path.insert(0, os.getcwd())  # python script.py 的 sys.path[0] 是脚本目录
    import benchmark.conftest as C
    import benchmark.base as B

    cfg = C.BenchConfig()
    cfg.warm_up = warmup
    cfg.repetition = rep
    cfg.user_desired_dtypes = [DTYPES[dtype_str]]
    shape_file = FRAMEWORK_SHAPE_FILE
    if op in _FW_SHAPE_OVERRIDE:
        # 按 op_name 覆盖 shape（base.Benchmark.set_shapes 先查 op_name key）
        shape_file = f"/tmp/baseline_shapes_{op}.yaml"
        with open(shape_file, "w", encoding="utf-8") as f:
            f.write(f"{op}:\n  shapes: {_FW_SHAPE_OVERRIDE[op]}\n")
    cfg.shape_file = shape_file
    C.Config = cfg
    B.Config = cfg  # 双保险：base 是 from .conftest import Config 的加载时绑定，强制覆盖

    pairs = []
    orig = B.Benchmark.get_latency

    def _rec(self, op_fn, *args, **kwargs):
        lat = orig(self, op_fn, *args, **kwargs)
        pairs.append((op_fn is self.torch_op, lat))
        return lat

    B.Benchmark.get_latency = _rec
    B.Benchmark.set_more_shapes = lambda self: []

    import importlib
    m = importlib.import_module(f"benchmark.test_{op}")
    fn_name = f"test_{op}"
    if not hasattr(m, fn_name):
        # 部分算子主体函数名带 dispatch 后缀（test_where_self），取最短的 test_* 为主体
        cands = [a for a in dir(m) if a.startswith("test_")]
        if not cands:
            _die(f"框架兜底失败: test_{op} 模块无 test_* 函数")
        fn_name = min(cands, key=len)
    sys.stderr.write(f"[bench_op] framework fallback: {op} via {fn_name} "
                     f"shape_file={os.path.basename(shape_file)}\n")

    # 捕获 stdout：SUCCESS 行自带 torch/gems 延迟，是 Generic 族（自定义 run 路径）的唯一可靠来源
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        getattr(m, fn_name)()
    if pairs:
        t_lats = [l for is_t, l in pairs if is_t]
        g_lats = [l for is_t, l in pairs if not is_t]
        if t_lats and g_lats:
            return t_lats[-1], g_lats[-1]
    succ = [l for l in buf.getvalue().splitlines() if l.startswith("SUCCESS")]
    if not succ:
        _die(f"框架兜底未捕获到延迟对: {op}")
    nums = re.findall(r"SUCCESS\s+([\d.]+)\s+([\d.]+)", succ[-1])
    if not nums:
        _die(f"框架兜底 SUCCESS 行解析失败: {op}: {succ[-1][:80]}")
    return float(nums[0][0]), float(nums[0][1])


def _fallback_ref_check(op, dtype_str):
    """框架兜底层参照挂点（AA 伪代码：board/fallback_refs.README.md §3 / plan §3）。

    计时成功返回后调用，与计时解耦：build_inputs -> gems 侧重放 -> compare，
    返回需并入输出 JSON 的附加字段（correct/max_abs_err/correct_mode，异常时 note）。
    重放顺序：unjudgeable 短路 → no_exception 跳过 → gems_fn 特签闭包优先 →
    use_gems 上下文重放（与计时同 dispatcher 路径）→ 异常落四级 importlib resolver。
    未注册 op 或任何异常 -> correct 保持 null + note，绝不阻塞计时主路径（try/except 包全）。
    """
    try:
        import fallback_refs  # 与 bench_op 同目录（板上 scripts/；缺席则走 note 降级）
        # 注册表 lambda 以模块级名解析 torch，而 fallback_refs 顶层不 import torch，此处注入
        fallback_refs.torch = torch
        # gems 解析器 _gems_resolver 同样依赖模块级名（本文件顶层已 import flag_gems）
        fallback_refs.flag_gems = flag_gems
        if op not in fallback_refs.REFS:
            return {"note": f"fallback_refs: op '{op}' 未注册（43 条表外），correct 保持 null"}
        # 参照 shape 口径：框架层 shape 覆盖表优先（yaml list 串），否则主口径 64,512,512
        shape_hint = _FW_SHAPE_OVERRIDE.get(op, "64,512,512")
        shape = (tuple(json.loads(shape_hint)[0]) if shape_hint.startswith("[")
                 else tuple(int(x) for x in shape_hint.split(",")))
        inputs = fallback_refs.build_inputs(op, shape, DTYPES[dtype_str], "cpu")
        entry = fallback_refs.REFS[op]
        if entry.mode == fallback_refs.MODE_UNJUDGEABLE:
            # 计划内恒 null（sparse_semi_structured_mm）：compare 前短路，
            # 不重放不比对，note 不被解析降级噪声污染
            return {"correct": None, "correct_mode": fallback_refs.MODE_UNJUDGEABLE}
        if entry.mode == fallback_refs.MODE_NO_EXCEPTION:
            # no_exception 只验 entry.fn(*inputs) 不抛异常（compare 不用 gems_out），
            # 跳过 gems 重放——板上本就无 assert_async 等控制流 gems 实现
            gems_out = None
        elif entry.gems_fn is not None:
            # 特签闭包优先：fused_adam（列表包装）/dropout（(out,mask) 元组）/
            # full/linspace/logspace（dtype/device keyword-only，直接调 TypeError）
            gems_out = entry.gems_fn(*inputs)
        else:
            try:
                # 主路径（2026-09-08 修复 A，板上已验证 bitwise_and 精确/softmax
                # 1.5e-8）：use_gems 上下文重放参照 fn——与计时同 dispatcher 路径，
                # torch.* 调用自动路由 flag_gems 注册实现
                with flag_gems.use_gems():
                    gems_out = entry.fn(*inputs)
            except Exception as exc:
                # fallback：四级 resolver 直调（顶层→ops→fused→importlib ops 子模块）
                fn = fallback_refs._gems_resolver(op)
                if fn is None:
                    return {"note": f"fallback_refs: use_gems 重放异常({type(exc).__name__}: "
                                    f"{exc})且 gems 侧四级解析均 miss '{op}'，correct 保持 null"}
                gems_out = fn(*inputs)
        res = fallback_refs.compare(op, inputs, gems_out)
        extra = {"correct": res["correct"]}
        if res.get("max_abs_err") is not None:
            extra["max_abs_err"] = round(float(res["max_abs_err"]), 6)
        if res.get("correct_mode") is not None:
            extra["correct_mode"] = res["correct_mode"]
        if res.get("error"):
            extra["note"] = f"fallback_refs: {res['error']}"
        return extra
    except Exception as exc:  # noqa: BLE001 挂点失败不阻塞计时输出
        return {"note": f"fallback_refs 挂点失败({type(exc).__name__}: {exc}): {op}"}


UNARY = {"abs", "neg", "exp", "sqrt", "rsqrt", "sigmoid", "tanh",
         "relu", "silu", "gelu"}
REDUCE = {"sum", "mean"}
ARG = {"argmax", "argmin"}
# dim 敏感算子：softmax(x, dim)；layer_norm/rms_norm 当前有已知 bug（P1 待修），
# 调用失败本身就是修复任务的起点证据
DIM_OPS = {"softmax"}
NORM_OPS = {"layer_norm", "rms_norm"}

DTYPES = {
    "float32": torch.float32, "float16": torch.float16,
    "int32": torch.int32, "int64": torch.int64, "bool": torch.bool,
}


def _make_input(shape, dtype):
    if dtype.is_floating_point:
        return torch.randn(shape, dtype=dtype)
    if dtype == torch.bool:
        return torch.rand(shape) > 0.5
    return torch.randint(-100, 100, shape, dtype=dtype)


def make_fn(op: str, a, b, dim):
    t = torch
    if op in BINARY:
        return (lambda: getattr(t, op)(a, b)), (lambda: getattr(flag_gems, op)(a, b))
    if op in UNARY:
        tfn = _TORCH_FN.get(op, lambda x: getattr(t, op)(x))
        return (lambda: tfn(a)), (lambda: getattr(flag_gems, op)(a))
    if op in REDUCE:
        return (lambda: getattr(t, op)(a)), (lambda: getattr(flag_gems, op)(a))
    if op in ARG:
        # dim=None 拉平路径，与 benchmark/test_argmax.py 的 torch.argmax(x) 一致
        return (lambda: getattr(t, op)(a)), (lambda: getattr(flag_gems, op)(a))
    if op in DIM_OPS:
        return (lambda: getattr(t, op)(a, -1)), (lambda: getattr(flag_gems, op)(a, -1))
    if op in NORM_OPS:
        ns = a.shape[-1]
        if op == "layer_norm":
            # 返回 (out, mean, rstd) 三元组，与 torch.layer_norm 单输出对齐取 [0]
            return ((lambda: t.layer_norm(a, (ns,))),
                    (lambda: flag_gems.layer_norm(a, (ns,))[0]))
        return ((lambda: t.nn.functional.rms_norm(a, (ns,))),
                (lambda: flag_gems.rms_norm(a, (ns,))))
    # 通用兜底：按 flag_gems 顶层签名推断调用方式
    return _generic_fn(op, a, b)


def _outputs_match(out_ref, out_gems, dtype):
    """宽松一致性：支持 tensor / 标量(bool,int,float) / tuple(list) 输出。

    返回 (correct, max_abs_err)；无法比较时返回 (None, None)。
    """
    rt, at = (5e-2, 5e-2) if dtype == torch.float16 else (5e-3, 5e-3)
    if torch.is_tensor(out_ref) and torch.is_tensor(out_gems):
        ok = bool(torch.allclose(
            out_ref.float(), out_gems.float(), rtol=rt, atol=at))
        diff = (out_ref.float() - out_gems.float()).abs()
        return ok, (float(diff.max()) if diff.numel() else 0.0)
    scalar = (bool, int, float)
    if isinstance(out_ref, scalar) and isinstance(out_gems, scalar):
        return bool(out_ref == out_gems), 0.0
    if isinstance(out_ref, (list, tuple)) and isinstance(out_gems, (list, tuple)):
        if len(out_ref) != len(out_gems):
            return False, None
        oks, max_err = [], 0.0
        for r, g in zip(out_ref, out_gems):
            if torch.is_tensor(r) and torch.is_tensor(g):
                if r.shape != g.shape:
                    return False, None
                ok_r, err_r = _outputs_match(r, g, dtype)
                if ok_r is None:
                    return None, None
                oks.append(ok_r)
                max_err = max(max_err, err_r)
            elif isinstance(r, scalar) and isinstance(g, scalar):
                oks.append(bool(r == g))
            else:
                return None, None
        return all(oks), max_err
    return None, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--op", required=True)
    ap.add_argument("--shape", required=True, help="逗号分隔，如 64,512,512")
    ap.add_argument("--dtype", default="float32", choices=list(DTYPES))
    ap.add_argument("--dim", type=int, default=0, help="argmax/argmin 的 dim")
    ap.add_argument("--warmup", type=int, default=25)
    ap.add_argument("--rep", type=int, default=100)
    ap.add_argument("--candidate-dir", default=None,
                    help="候选目录：其中 <op>.py（或唯一 *<op>*.py）以 flag_gems.ops.<op> 身份加载")
    ap.add_argument("--skip-torch", action="store_true",
                    help="跳过 torch 参照计时臂（只跳计时，不跳参照输出）：torch_ms/speedup 输出 null "
                         "并显式写 torch_skipped=true；correct/max_abs_err 不受影响。"
                         "框架兜底路径成对计时不支持跳过（此处为 no-op）")
    ap.add_argument("--seed", type=int, default=0,
                    help="输入播种 torch.manual_seed(seed)，作用于 _make_input 的 "
                         "randn/rand/randint；默认 0 与历史行为一致。seed 非 0 时输出行"
                         "带 \"seed\" 键（默认输出逐字节不变，seed 轮换用）")
    args = ap.parse_args()

    if args.candidate_dir:
        # flag_gems 未在初始化期 import 该 ops 子模块时补载（正常路径钩子在
        # import flag_gems 期间已命中，不进此分支）
        import importlib
        modname = f"flag_gems.ops.{args.op}"
        if modname not in sys.modules:
            sys.stderr.write(f"[bench_op] candidate-dir: 补载 {modname}（非初始化期路径，"
                             f"flag_gems 顶层绑定可能未指向候选）\n")
            importlib.import_module(modname)

    torch.manual_seed(args.seed)  # 输入播种：--seed 轮换（默认 0 与历史行为一致）
    shape_str = _SPECIAL_SHAPE.get(args.op, args.shape)
    shape = tuple(int(x) for x in shape_str.split(","))
    dtype = DTYPES[args.dtype]
    a = _make_input(shape, dtype)
    b = _make_input(shape, dtype) if args.op in BINARY else None

    ref_fn, gems_fn = make_fn(args.op, a, b, args.dim)

    out_ref = out_gems = None
    if ref_fn is not None:
        try:
            # 预热 + 探测：直调任一侧失败（签名/输入构造不匹配）即落框架兜底
            ref_fn(); gems_fn()
            out_ref = ref_fn(); out_gems = gems_fn()
            if not (torch.is_tensor(out_ref) or isinstance(out_ref, (bool, int, float, list, tuple))):
                raise TypeError(f"不可比的输出类型 {type(out_ref).__name__}")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"[bench_op] 直调预热失败({type(exc).__name__}: {exc})，转框架兜底: {args.op}\n")
            ref_fn, out_ref, out_gems = None, None, None

    if ref_fn is None:
        # bench_op 分发不了的算子：走 FlagGems benchmark 框架兜底（真实 do_bench）
        lat_t, lat_g = _framework_bench(args.op, args.dtype, args.warmup, args.rep)
        # 挂点：计时成功后重放 fallback_refs 注册表参照（失败/未注册 correct 保持 null）
        print(json.dumps({
            "op": args.op, "shape": "framework", "dtype": args.dtype,
            "torch_ms": round(lat_t, 4), "gems_ms": round(lat_g, 4),
            "speedup": round(lat_t / lat_g, 4) if lat_g > 0 else None,
            "correct": None, "framework": True,
            **_fallback_ref_check(args.op, args.dtype),
            # seed 非 0 时显式记录（seed 轮换实验可追溯；默认输出逐字节不变）
            **({"seed": args.seed} if args.seed else {}),
            # --skip-torch 在框架兜底路径为 no-op（FlagGems benchmark 的 get_latency
            # 是 torch/gems 成对记录，拆不开）；仅在显式传旗标时写明，默认输出逐字节不变
            **({"torch_skipped": False, "skip_torch_ignored": "framework"}
               if args.skip_torch else {}),
        }))
        return

    if args.skip_torch:
        # 只跳计时臂：out_ref（L493-504 探测与参照输出）已生成，correct/max_abs_err 判定不受影响
        lat_t = None
    else:
        lat_t = triton.testing.do_bench(
            ref_fn, warmup=args.warmup, rep=args.rep, return_mode="median")
    lat_g = triton.testing.do_bench(
        gems_fn, warmup=args.warmup, rep=args.rep, return_mode="median")

    if args.op in _NO_CORRECT:
        correct, max_err = None, None
    elif args.op in ARG:  # 索引类：精确相等（allclose 相对容差会掩盖 off-by-1）
        correct = bool(torch.equal(out_ref, out_gems))
        max_err = 0.0
    else:
        correct, max_err = _outputs_match(out_ref, out_gems, dtype)

    print(json.dumps({
        "op": args.op, "shape": list(a.shape), "dtype": args.dtype,
        "torch_ms": None if lat_t is None else round(lat_t, 4),
        "gems_ms": round(lat_g, 4),
        "speedup": round(lat_t / lat_g, 4) if (lat_t is not None and lat_g > 0) else None,
        "correct": correct,
        **({} if max_err is None else {"max_abs_err": round(max_err, 6)}),
        # seed 非 0 时显式记录（与 framework 行同口径；默认输出逐字节不变）
        **({"seed": args.seed} if args.seed else {}),
        # 显式标记键（不静默省略——下游读 None 与读"键不存在"语义不同）；
        # 不带旗标时不写该键，输出与改前逐字节一致
        **({"torch_skipped": True} if args.skip_torch else {}),
    }))


if __name__ == "__main__":
    main()
