#!/usr/bin/env python
"""框架兜底层 torch 参照注册表（43 op）—— bench_op 扩展的数值判定正本。

规格依据：results/panel/fallback-reference-plan.md（A 直构 37 / B 组构 2 / C 不可构 4）。
本文件只被 bench_op 挂点 import，严禁在本文件内改 board/bench_op.py（挂点另行接入）。

依赖与扩展约定：
- 数值合同：运行期读 ``results/panel/numeric-contract.csv``（相对本文件 ../results/panel/，
  即 repo 根），CSV 缺席时（板上路径白名单）回退内嵌 ``_CONTRACT_FALLBACK``
  （2026-09-08 由该 CSV 编译）。CSV 变更后无需重生成代码，运行期即生效。
- fp16 扩展：只改 CONTRACT（按 dtype 取值），**不动注册表**（本文件头部即为约定正本）。
  未命中 CONTRACT 的 op 回退 bench_op 现行口径：fp32 (5e-3, 5e-3) / fp16 (5e-2, 5e-2)。
- torch 调用习惯：一律 ``F.*`` 优先（已知坑：silu/gelu/sigmoid/tanh 的 torch. 顶层名在
  板上被 patch，本地虽无此 patch 也保持一致；本批 43 op 不含这四个）。Aten 私有名直调
  条目（_chunk_cat / _unsafe_masked_index / _jagged_to_padded_dense_forward /
  _functional_sym_constrain_range / _assert_async）均逐条注明，且给出公开组合等价式降级
  （板上已证可调用才保留直调）。

CLI：
  python board/fallback_refs.py --list           # 打印注册表与 mode 分布
  python board/fallback_refs.py --selftest       # 不依赖 torch 的结构自测
  python board/fallback_refs.py --selftest-torch # 依赖 torch 的构建/比对自测（无 torch 则 SKIP）
"""

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = ["RefEntry", "REFS", "CONTRACT", "compare", "build_inputs", "main"]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAIN_SHAPE = (64, 512, 512)  # baseline_shapes.yaml 主口径（16.7M 元素档）

# mode 词汇表（分布 37/2/2/1/1，--selftest 校验）
MODE_CONTRACT = "contract"          # 数值合同判定（rtol=atol=0 时退 torch.equal）
MODE_EXACT = "exact"                # 定义性精确（bitwise int32 / 纯形状变换），torch.equal
MODE_NO_EXCEPTION = "no_exception"  # C 类 control_flow：只验证不抛异常
MODE_STATISTICAL = "statistical"    # C 类 stochastic（dropout）：统计校验
MODE_UNJUDGEABLE = "unjudgeable"    # C 类：correct 恒 None（sparse_semi_structured_mm）

_VALID_MODES = {MODE_CONTRACT, MODE_EXACT, MODE_NO_EXCEPTION,
                MODE_STATISTICAL, MODE_UNJUDGEABLE}

# 注册表正本 43 条（顺序 = fallback-reference-plan.md §1 清单）
CANONICAL_OPS = [
    "add_relu", "as_strided_copy", "assert_async", "batch_norm_no_update",
    "bitwise_and", "channel_shuffle", "chunk", "chunk_cat", "dropout", "flip",
    "full", "full_like", "functional_sym_constrain_range", "fused_adam",
    "greater_equal", "group_norm", "index_copy", "jagged_to_padded_dense_forward",
    "kthvalue", "less", "linspace", "logspace", "make_dep_token",
    "margin_ranking_loss", "narrow", "permute_copy", "pixel_shuffle",
    "prelu_kernel", "renorm", "resize_output", "safe_softmax", "select_scatter",
    "sparse_semi_structured_mm", "split_with_sizes_copy", "tensor_split",
    "threshold", "tile", "transpose", "unfold_copy", "unfold_view",
    "unsafe_masked_index", "unsafe_view", "unsqueeze",
]

# C 类名单（fn 允许为 None 的条目）
_FN_NONE_OK = {"sparse_semi_structured_mm"}


# ---------------------------------------------------------------------------
# 数值合同 CONTRACT
# ---------------------------------------------------------------------------

# 内嵌回退表：2026-09-08 从 results/panel/numeric-contract.csv 编译（43 op 中
# dropout 的 rtol/atol 为 '-' 即 statistical，不入表 → 42 条）。
_CONTRACT_FALLBACK = {
    "add_relu": (1.3e-6, 1e-4),
    "as_strided_copy": (0.0, 0.0),
    "assert_async": (0.0, 0.0),
    "batch_norm_no_update": (1.3e-6, 1e-4),
    "bitwise_and": (0.0, 0.0),
    "channel_shuffle": (0.0, 0.0),
    "chunk": (0.0, 0.0),
    "chunk_cat": (0.0, 0.0),
    "flip": (0.0, 0.0),
    "full": (0.0, 0.0),
    "full_like": (0.0, 0.0),
    "functional_sym_constrain_range": (0.0, 0.0),
    "fused_adam": (1.3e-6, 1e-4),
    "greater_equal": (0.0, 0.0),
    "group_norm": (1.3e-6, 1e-4),
    "index_copy": (0.0, 0.0),
    "jagged_to_padded_dense_forward": (0.0, 0.0),
    "kthvalue": (0.0, 0.0),
    "less": (0.0, 0.0),
    "linspace": (1.3e-6, 1e-4),
    "logspace": (1.3e-6, 1e-4),
    "make_dep_token": (0.0, 0.0),
    "margin_ranking_loss": (1.3e-6, 1e-4),
    "narrow": (0.0, 0.0),
    "permute_copy": (0.0, 0.0),
    "pixel_shuffle": (0.0, 0.0),
    "prelu_kernel": (1.3e-6, 1e-4),
    "renorm": (1.3e-6, 1e-4),
    "resize_output": (0.0, 0.0),
    "safe_softmax": (1.3e-6, 1e-4),
    "select_scatter": (0.0, 0.0),
    "sparse_semi_structured_mm": (1.3e-6, 1e-4),
    "split_with_sizes_copy": (0.0, 0.0),
    "tensor_split": (0.0, 0.0),
    "threshold": (1.3e-6, 1e-4),
    "tile": (0.0, 0.0),
    "transpose": (0.0, 0.0),
    "unfold_copy": (0.0, 0.0),
    "unfold_view": (0.0, 0.0),
    "unsafe_masked_index": (0.0, 0.0),
    "unsafe_view": (0.0, 0.0),
    "unsqueeze": (0.0, 0.0),
}

_CSV_PATH = Path(__file__).resolve().parent.parent / "results" / "panel" / "numeric-contract.csv"


def _load_contract():
    """优先运行期读 numeric-contract.csv；读不到回退内嵌表（板上白名单场景）。

    rtol/atol 列为 '-'（stochastic 类）的行跳过。CSV 存在但缺某 43-op 行时用内嵌表补齐。
    """
    out = {}
    try:
        with open(_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                op = (row.get("op") or "").strip()
                if op not in CANONICAL_OPS:
                    continue
                try:
                    out[op] = (float(row["rtol"]), float(row["atol"]))
                except (KeyError, ValueError):
                    continue  # '-'（stochastic）或空值
    except OSError:
        return dict(_CONTRACT_FALLBACK)
    for op, val in _CONTRACT_FALLBACK.items():
        out.setdefault(op, val)
    return out


CONTRACT = _load_contract()


# ---------------------------------------------------------------------------
# RefEntry
# ---------------------------------------------------------------------------

@dataclass
class RefEntry:
    """单条 torch 参照注册项。

    fn             torch 参照函数，签名 = input_builder 产出顺序（可为 None：C 类）。
    input_builder  (shape, dtype, device) -> tuple，内部 torch.manual_seed(0)，fp32 主口径。
    gems_fn        gems 侧闭包覆盖（fused_adam 等列表包装签名）；缺省 = _gems_resolver(op)
                   （顶层 → ops → fused 三级解析）。
    out_adapter    (ref_out, gems_out) -> 归一化可比对 (r, g)；缺省恒等。
    mode           contract | exact | no_exception | statistical | unjudgeable。
    reduce_scale_fn atol 缩放系数 fn(inputs)（归约缩放类：group_norm/safe_softmax），None = x1。
    note           特判/坑位说明（Aten 私有名直调等）。
    """
    fn: Optional[Callable]
    input_builder: Callable
    gems_fn: Optional[Callable] = None
    out_adapter: Optional[Callable] = None
    mode: str = MODE_CONTRACT
    reduce_scale_fn: Optional[Callable] = None
    note: str = ""


# ---------------------------------------------------------------------------
# input_builder 基件与通用造数（均 lazy import torch；manual_seed(0) 内建）
# ---------------------------------------------------------------------------

def _t(shape, dtype, device):
    import torch
    return torch.randn(list(shape), dtype=dtype, device=device)


def _seed_randn(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    """返回 (x,)：单随机输入。"""
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device),)


def _seed_binary(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    """返回 (a, b)：双随机输入。"""
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), _t(shape, dtype, device))


# ---------------------------------------------------------------------------
# A 类参照的降级/适配 helper（Aten 私有名直调条目）
# ---------------------------------------------------------------------------

def _dep_token():
    """best-effort dep_token 构造；私有 API 链均失败返回 None（compare 判 correct=None，不误报）。"""
    import torch
    for ctor in (lambda: torch._C._make_dep_token(),
                 lambda: torch.ops.aten.make_dep_token.default()):
        try:
            return ctor()
        except Exception:  # noqa: BLE001
            continue
    return None


def _chunk_cat_ref(x, dim, n):
    try:
        return torch._chunk_cat([x], dim, n)  # Aten 私有名直调（test 原样）
    except (AttributeError, RuntimeError):
        return torch.cat(torch.chunk(x, n, dim), dim)  # 公开组合等价式


def _permute_copy_ref(x, dims):
    try:
        return torch.permute_copy(x, dims)  # 公开 aten 口径（test 原样）
    except (AttributeError, RuntimeError):
        return x.permute(dims).clone()  # 组合等价式


def _unfold_copy_ref(x, dim, size, step):
    try:
        return torch.unfold_copy(x, dim, size, step)  # 公开 aten 口径（test 原样）
    except (AttributeError, RuntimeError):
        return x.unfold(dim, size, step).clone()  # 组合等价式


def _unsafe_masked_index_ref(x, mask, indices, fill):
    try:
        return torch._unsafe_masked_index(x, mask, indices, fill)  # Aten 私有名（test 原样）
    except (AttributeError, RuntimeError):
        idx = indices[0]
        return torch.where(mask, x[idx],
                           torch.as_tensor(fill, dtype=x.dtype))  # 公开组合等价式


def _fsymcr_ref(size, lo, hi, dep):
    # Aten 私有名直调（test 原样）；控制面 op：dep_token/size 透传（=恒等）
    return torch.ops.aten._functional_sym_constrain_range(size, lo, hi, dep)


def _jagged_ref(values, offsets, max_length, padding):
    # Aten 私有名直调（test 原样）；公开组合无等价式（NT 内部布局），失败则 compare 报 error
    return torch.ops.aten._jagged_to_padded_dense_forward(
        values, [offsets], list(max_length), padding)


def _assert_async_ref(x):
    # Aten 私有名直调（test 原样 torch._assert_async）；全真输入正常返回 None
    return torch._assert_async(x)


def _kthvalue_adapter(r, g):
    """kthvalue 只比值（CSV：索引平局 CUDA 不确定，不入比对）。"""
    import torch
    rv = r.values if not torch.is_tensor(r) else r
    if torch.is_tensor(g):
        gv = g
    elif hasattr(g, "values"):
        gv = g.values
    elif isinstance(g, (list, tuple)):
        gv = g[0]
    else:
        gv = g
    return rv, gv


# ---------------------------------------------------------------------------
# gems 侧重放函数解析器（flag_gems 由挂点注入为模块级名后可用；本文件顶层不 import）
# ---------------------------------------------------------------------------

def _ops_submodule_fn(op):
    """第 4 级：importlib 直入 ``flag_gems.ops`` 子模块取重放函数。

    必须走 importlib——``flag_gems.ops.<op>`` 的属性访问会被 ops 包的 ``__getattr__``
    拦截重定向；importlib.import_module 绕过包属性查找直达子模块。
    模块内候选名（bench_op 直调层 where 系 ``_func`` 后缀同款）：<op> / _<op> / <op>_func。
    """
    import importlib
    for mod_name in (f"flag_gems.ops.{op}", f"flag_gems.ops._{op}"):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 ImportError/子模块自身 import 失败一律降级
            continue
        for name in (op, f"_{op}", f"{op}_func"):
            fn = getattr(mod, name, None)
            if callable(fn):
                return fn
    return None


def _gems_resolver(op):
    """gems 侧重放函数通用解析，四级顺序：顶层 ``flag_gems.<op>`` → ops 命名空间
    （``flag_gems.ops.<op>`` 可能是子模块，取模块内同名函数，bench_op 通用直调层同款）
    → ``flag_gems.fused.<op>``（fused_adam 所在，板上 pin 无顶层导出）
    → importlib 直入 ops 子模块（``flag_gems.ops.<op>`` / ``._<op>``，候选 <op>/_<op>/<op>_func）。
    全 miss 返回 None（挂点走 note 降级，correct 保持 null）。"""
    fg = globals().get("flag_gems")
    if fg is None:
        return None
    fn = getattr(fg, op, None)
    if fn is None:
        ops_ns = getattr(fg, "ops", None)
        cand = getattr(ops_ns, op, None) if ops_ns is not None else None
        if cand is not None and not callable(cand):
            cand = getattr(cand, op, None)   # ops/<op>.py 子模块内同名函数
        fn = cand
    if fn is None:
        fused_ns = getattr(fg, "fused", None)
        fn = getattr(fused_ns, op, None) if fused_ns is not None else None
    if fn is None:
        fn = _ops_submodule_fn(op)           # 第 4 级：importlib 子模块直入
    return fn if callable(fn) else None


# ---------------------------------------------------------------------------
# 签名适配：gems 侧闭包（2026-09-08 复验修复 B/C；挂点顺序 = gems_fn 优先，
# 无 gems_fn 走 bench_op 挂点的 use_gems 重放，importlib resolver 只作兜底）
# ---------------------------------------------------------------------------

def _resolve_or_raise(op):
    fn = _gems_resolver(op)
    if fn is None:
        raise RuntimeError(f"flag_gems 四级解析均取不到 {op}")
    return fn


def _full_gems(size, fill_value, dt, dev):
    """flag_gems.full 只收 2 位置参（dtype/device keyword-only，板上实证）。"""
    return _resolve_or_raise("full")(size, fill_value, dtype=dt, device=dev)


def _linspace_gems(start, end, steps, dt, dev):
    return _resolve_or_raise("linspace")(start, end, steps, dtype=dt, device=dev)


def _logspace_gems(start, end, steps, base, dt, dev):
    return _resolve_or_raise("logspace")(start, end, steps, base=base,
                                         dtype=dt, device=dev)


def _dropout_gems(x, p):
    """板上 flag_gems.dropout(x, p) 返回 (out, mask) 元组——原样上交，
    由 out_adapter 取 [0] 后再进统计校验。"""
    return _resolve_or_raise("dropout")(x, p)


def _dropout_adapter(r, g):
    """dropout out_adapter：gems 侧 (out, mask) 元组取 out（statistical 只用 out）。"""
    if isinstance(g, (list, tuple)) and len(g) > 0:
        return r, g[0]
    return r, g


# ---------------------------------------------------------------------------
# B 类：fused_adam（抄 test_fused_adam.py 内置 torch_op 组合语义）
# ---------------------------------------------------------------------------

def _fused_adam_ref(params, grads, exp_avgs, exp_avg_sqs, lr, beta1, beta2, eps):
    """逐张量组合参照（test 同款）：m=β1*m+(1-β1)*g; v=β2*v+(1-β2)*g*g;
    param-=lr*(m/(1-β1**t))/(sqrt(v/(1-β2**t))+eps)；t=1（state_steps=1，builder 固定）。
    返回新 param 列表，不原地改（避免污染计时输入）。"""
    t = 1
    out = []
    for p, g, m0, v0 in zip(params, grads, exp_avgs, exp_avg_sqs):
        m = beta1 * m0 + (1 - beta1) * g
        v = beta2 * v0 + (1 - beta2) * g * g
        m_hat = m / (1 - beta1 ** t)
        v_hat = v / (1 - beta2 ** t)
        out.append(p - lr * m_hat / (torch.sqrt(v_hat) + eps))
    return out


def _fused_adam_gems(params, grads, exp_avgs, exp_avg_sqs, lr, beta1, beta2, eps):
    """gems 侧：列表包装签名（test set_gems 闭包同款）；兼容原地/返回两种实现。

    fused_adam 挂载点经 _gems_resolver 解析（板上 pin 无顶层导出，实际在 fused 子模块）。
    """
    fn = _gems_resolver("fused_adam")
    if fn is None:
        raise RuntimeError("flag_gems 顶层/ops/fused 三级均解析不到 fused_adam")
    p = [x.clone() for x in params]
    g = [x.clone() for x in grads]
    m = [x.clone() for x in exp_avgs]
    v = [x.clone() for x in exp_avg_sqs]
    z = [torch.zeros_like(x) for x in params]            # max_exp_avg_sqs（amsgrad=False 占位）
    s = [torch.tensor(1) for _ in params]                # state_steps=1（与 ref 的 t=1 对齐）
    out = fn(p, g, m, v, z, s, lr=lr, beta1=beta1, beta2=beta2,
             weight_decay=0.0, eps=eps, amsgrad=False, maximize=False)
    # 板上 2026-09-08 实证：kernel 返回 5 元组 (params, grads, exp_avgs,
    # exp_avg_sqs, max_exp_avg_sqs)，out[0] 为原地更新的 params 列表
    if isinstance(out, (list, tuple)):
        if out and isinstance(out[0], (list, tuple)):
            return list(out[0])
        return list(out)
    return p


def _b_fused_adam(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    p = [torch.randn(list(shape), dtype=dtype, device=device)]
    g = [torch.randn(list(shape), dtype=dtype, device=device)]
    m = [torch.zeros_like(p[0])]   # 一阶矩初值 0（首步语义）
    v = [torch.zeros_like(p[0])]   # 二阶矩初值 0
    return (p, g, m, v, 1e-3, 0.9, 0.999, 1e-8)


# ---------------------------------------------------------------------------
# input_builders（类特判/硬表条目；默认签名统一为 (shape, dtype, device)）
# ---------------------------------------------------------------------------

def _b_as_strided(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # test 硬表 5 组取代表；基址多 8 元素 + offset=4，size/stride 对齐 [64,512] 主口径
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    base = torch.randn(64 * 512 + 8, dtype=dtype, device=device)
    return (base, (64, 512), (512, 1), 4)


def _b_batch_norm(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # 输入元组 (x, w, b, rm, rv, 0.1, 1e-5)，C=shape[1]
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    c = shape[1]
    x = _t(shape, dtype, device)
    w = torch.randn(c, dtype=dtype, device=device)
    b = torch.randn(c, dtype=dtype, device=device)
    rm = torch.randn(c, dtype=dtype, device=device)
    rv = torch.rand(c, dtype=dtype, device=device) + 0.5
    return (x, w, b, rm, rv, 0.1, 1e-5)


def _b_bitwise_and(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # int32 主口径（CSV：int32 位运算精确），dtype 参数忽略
    import torch
    torch.manual_seed(0)
    a = torch.randint(-2 ** 20, 2 ** 20, list(shape), dtype=torch.int32, device=device)
    b = torch.randint(-2 ** 20, 2 ** 20, list(shape), dtype=torch.int32, device=device)
    return (a, b)


def _b_channel_shuffle(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # test 硬表 3 组取代表；channel_shuffle 需 4D (N,C,H,W)，C 可被 groups 整除
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = torch.randn(4, 64, 64, 64, dtype=dtype, device=device)
    return (x, 4)


def _b_chunk(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 3, 0)  # chunks=3, dim=0（test 口径）


def _b_chunk_cat(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 0, 4)  # dim=0, num_chunks∈{2,4} 取 4


def _b_dropout(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # (x, 0.5)：gems 侧 flag_gems.dropout(x, 0.5)（p 为必选位置参；training 缺省 True）
    return (*_seed_randn(shape, dtype, device), 0.5)


def _b_flip(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    dims = (0,) if x.ndim < 2 else (0, 1)  # 1D 用 (0,)
    return (x, dims)


def _b_full(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    return (list(shape), 3.1415926, dtype, device)  # 无随机性，seed 无需


def _b_full_like(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 3.1415926)


def _b_fsymcr(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    torch.manual_seed(0)  # 恒等控制面，无随机张量
    return (5, 1, 10, _dep_token())


def _b_group_norm(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # 输入元组 (x, G, w, b)，G = C//2
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    c = shape[1]
    g = max(1, c // 2)
    x = _t(shape, dtype, device)
    w = torch.randn(c, dtype=dtype, device=device)
    b = torch.randn(c, dtype=dtype, device=device)
    return (x, g, w, b)


def _group_norm_scale(inputs):
    # atol_eff = atol × 组内元素数 = (C/G)×∏spatial（CSV special_rules）
    x, g = inputs[0], inputs[1]
    spatial = 1
    for d in x.shape[2:]:
        spatial *= d
    return (x.shape[1] // g) * spatial


def _safe_softmax_scale(inputs):
    # atol_eff = atol × reduce_dim = 末维大小（plan §3.3）
    return inputs[0].shape[-1]


def _b_index_copy(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    n = min(32, shape[0])  # selftest 小 shape（<32）也自洽：indices 数 = src.size(dim)
    idx = torch.randperm(shape[0], device=device)[:n]
    src = torch.randn(n, *shape[1:], dtype=dtype, device=device)
    return (x, 0, idx, src)


def _b_jagged(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # offsets 由固定种子生成（plan）；D=64、B=8、max_length=100
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    lengths = torch.randint(1, 100, (8,), device=device)
    offsets = torch.cat([torch.zeros(1, dtype=torch.int64, device=device),
                         torch.cumsum(lengths, 0)]).to(torch.int32)
    values = torch.randn(int(offsets[-1]), 64, dtype=dtype, device=device)
    return (values, offsets, [100], 0.0)


def _b_linspace(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    return (0.0, 2.0, 4096, dtype, device)  # steps 取代表档（参照比对与 shape 无关）


def _b_logspace(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    return (0.0, 6.0, 4096, 1.05, dtype, device)


def _b_margin_ranking(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # 输入元组 (x1, x2, target∈{-1,+1}, 0.5, 1)；输出标量（reduction='mean'）
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    n = 4096
    x1 = torch.randn(n, dtype=dtype, device=device)
    x2 = torch.randn(n, dtype=dtype, device=device)
    target = torch.where(torch.randn(n, device=device) > 0, 1.0, -1.0)
    return (x1, x2, target, 0.5, 1)


def _b_narrow(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    return (x, 0, x.shape[0] // 4, x.shape[0] // 2)  # start=len//4, length=len//2


def _b_permute_copy(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), (2, 0, 1))  # 硬表 3 组取代表


def _b_pixel_shuffle(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    # core_shapes 口径取代表：4D (N, C*r^2, H, W)，r=2
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = torch.randn(2, 64, 128, 128, dtype=dtype, device=device)
    return (x, 2)


def _b_prelu(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    w = torch.randn(1, dtype=dtype, device=device)  # 单共享权重（BinaryPointwise 口径）
    return (x, w)


def _b_renorm(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 2.0, 1, 1.0)  # reduce_dim=1（CSV）


def _b_resize_output(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    return (x, [x.numel()])  # target_size 与 numel 恒等 → resize 退化为纯形状变换


def _b_safe_softmax(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), -1)


def _b_select_scatter(shape=(64, 512), dtype=None, device="cpu"):
    # TensorSelectBenchmark 2DOnly 口径；randidx 固定代表值
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = torch.randn(64, 512, dtype=dtype, device=device)
    src = torch.randn(512, dtype=dtype, device=device)
    return (x, src, 0, 7)


def _b_sparse_mm(shape=(64, 128), dtype=None, device="cpu"):
    # TODO(gemm): test 的 torch_op = gems 自身（自比），torch CPU 无 2:4 等价；
    # meta 编码语义未定 → 仅构造稠密候选 (mat1, mat2)，fn=None，correct 恒 None。
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    a = torch.randn(64, 128, dtype=dtype, device=device)
    b = torch.randn(128, 64, dtype=dtype, device=device)
    return (a, b)


def _b_split_sizes(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    n = shape[0]
    return (x, [n // 4, n // 4, n - 2 * (n // 4)], 0)


def _b_tensor_split(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 3)  # sections=3


def _b_threshold(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 3.14, 2.71)


def _b_tile(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    return (x, [2] + [1] * (x.ndim - 1))


def _b_transpose(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    return (x, 0, x.ndim - 1)


def _b_unfold_copy(shape=(64, 128, 32), dtype=None, device="cpu"):
    # test 硬表 8 组取代表（dim=1, size=8, step=4，主口径 3D 输入）
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 1, 8, 4)


def _b_unfold_view(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    return (_t(shape, dtype, device), 0, 4, 2)  # dimension=0, size=4, step=2


def _b_unsafe_masked_index(shape=(64, 512), dtype=None, device="cpu"):
    # test 语义：value/mask/indices 三者同 shape（1D n 档，indices∈[0,n)，mask 比例 0.3）。
    # 2026-09-08 修正：旧版 value 与 mask/indices 形状不一致，板上 use_gems 路由进
    # flag_gems kernel 后越界 -> spine_triton driver segfault（rc=-11 两次实证）
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    n = shape[0] if len(shape) == 1 else 64 * 512
    x = torch.randn(n, dtype=dtype, device=device)
    mask = torch.rand(n, device=device) > 0.3
    indices = torch.randint(0, n, (n,), device=device)
    return (x, mask, [indices], 0.0)


def _b_unsafe_view(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    return (x, [x.numel()])


def _b_unsqueeze(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    dtype = dtype or torch.float32
    torch.manual_seed(0)
    x = _t(shape, dtype, device)
    return (x, 1 if x.ndim >= 2 else 0)  # 1D→0，≥2D→1


def _b_assert_async(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    import torch
    # 单元素 bool 张量（0-dim）：_assert_async 对 >1 元素张量做真值判定会
    # RuntimeError("Boolean value of Tensor with more than one value is ambiguous")，
    # 板上 43 全量验证实证；0-dim 全真输入 torch/gems 均正常返回 None
    torch.manual_seed(0)
    return (torch.ones((), dtype=torch.bool, device=device),)


def _b_make_dep_token(shape=MAIN_SHAPE, dtype=None, device="cpu"):
    torch.manual_seed(0)  # 无张量输入；dep_token 由参照 fn 内部构造
    return ()


# ---------------------------------------------------------------------------
# REFS 注册表（43 条：A 37 / B 2 / C 4）
# ---------------------------------------------------------------------------

REFS = {
    # ===== A 类：直构（37 条，一行式 ref_fn；0/0 合同在 contract 路径走 torch.equal） =====
    "add_relu": RefEntry(
        lambda a, b: torch.relu(a + b), _seed_binary,
        note="contract 浮点容差"),
    "as_strided_copy": RefEntry(
        lambda a, size, stride, offset: torch.as_strided(a, size, stride, offset),
        _b_as_strided,
        note="as_strided 值口径 = as_strided_copy（视图即可值相等）；硬表 5 组取代表"),
    "batch_norm_no_update": RefEntry(
        lambda x, w, b, rm, rv, momentum, eps: torch.nn.functional.batch_norm(
            x, rm, rv, w, b, training=False, eps=eps),
        _b_batch_norm,
        note="推理态 BN（training=False，无统计归约），F.* 口径；reduce_dim=1 容差维持"),
    "bitwise_and": RefEntry(
        lambda a, b: torch.bitwise_and(a, b), _b_bitwise_and, mode=MODE_EXACT,
        note="int32 位运算：定义性逐位精确（不入浮点合同）"),
    "channel_shuffle": RefEntry(
        lambda x, groups: torch.channel_shuffle(x, groups), _b_channel_shuffle,
        note="硬表 3 组取 groups=4 代表；4D 输入"),
    "chunk": RefEntry(
        lambda x, chunks, dim: torch.chunk(x, chunks, dim), _b_chunk,
        note="输出 tuple，递归比对"),
    "chunk_cat": RefEntry(
        _chunk_cat_ref, _b_chunk_cat,
        note="Aten 私有名 _chunk_cat 直调（test 原样），降级 cat∘chunk"),
    "flip": RefEntry(
        lambda x, dims: torch.flip(x, list(dims)), _b_flip),
    "full": RefEntry(
        lambda size, fill_value, dt, dev: torch.full(size, fill_value, dtype=dt, device=dev),
        _b_full, gems_fn=_full_gems,
        note="gems 侧 keyword-only（dtype/device），直接调会 TypeError（板上实证）"),
    "full_like": RefEntry(
        lambda x, fill_value: torch.full_like(x, fill_value), _b_full_like),
    "functional_sym_constrain_range": RefEntry(
        _fsymcr_ref, _b_fsymcr,
        note="control_plane：Aten 私有名直调（test 原样）；dep_token 恒等透传，"
             "非张量输出按相等/恒等判定；dep 构造失败时 correct=None（不误报）"),
    "greater_equal": RefEntry(
        lambda x, y: torch.greater_equal(x, y), _seed_binary,
        note="输出 bool"),
    "group_norm": RefEntry(
        lambda x, g, w, b: torch.nn.functional.group_norm(x, g, w, b), _b_group_norm,
        reduce_scale_fn=_group_norm_scale,
        note="F.* 口径；atol×组内元素数=(C/G)×∏spatial"),
    "index_copy": RefEntry(
        lambda x, dim, index, src: torch.index_copy(x, dim, index, src), _b_index_copy,
        note="重复索引按 torch 后写覆盖语义（非 accumulate）"),
    "jagged_to_padded_dense_forward": RefEntry(
        _jagged_ref, _b_jagged,
        note="Aten 私有名直调（test 原样）；offsets 固定种子生成（int32）；无公开组合等价式"),
    "kthvalue": RefEntry(
        lambda x, k, dim: torch.kthvalue(x, k, dim=dim),
        lambda shape, dtype, device: (*_seed_randn(shape, dtype, device), 2, -1),
        out_adapter=_kthvalue_adapter,
        note="只比 values（索引平局 CUDA 不确定，不入比对）；k=2, dim=-1 固定"),
    "less": RefEntry(
        lambda x, y: torch.less(x, y), _seed_binary,
        note="输出 bool"),
    "linspace": RefEntry(
        lambda start, end, steps, dt, dev: torch.linspace(start, end, steps, dtype=dt, device=dev),
        _b_linspace, gems_fn=_linspace_gems,
        note="gems 侧 keyword-only（dtype/device），直接调会 TypeError（板上实证）"),
    "logspace": RefEntry(
        lambda start, end, steps, base, dt, dev: torch.logspace(
            start, end, steps, base=base, dtype=dt, device=dev),
        _b_logspace, gems_fn=_logspace_gems,
        note="gems 侧 keyword-only（base/dtype/device），直接调会 TypeError（板上实证）"),
    "margin_ranking_loss": RefEntry(
        lambda x1, x2, target, margin, reduction: torch.nn.functional.margin_ranking_loss(
            x1, x2, target, margin=margin,
            reduction={0: "none", 1: "mean", 2: "sum"}.get(reduction, "mean")),
        _b_margin_ranking,
        note="F.* 口径；输出标量，reduce_dim=1 容差维持"),
    "narrow": RefEntry(
        lambda x, dim, start, length: torch.narrow(x, dim, start, length), _b_narrow),
    "permute_copy": RefEntry(
        _permute_copy_ref, _b_permute_copy,
        note="硬表 3 组取 dims=(2,0,1) 代表；降级 permute().clone()"),
    "pixel_shuffle": RefEntry(
        lambda x, r: torch.nn.functional.pixel_shuffle(x, r), _b_pixel_shuffle,
        note="F.* 口径（=aten.pixel_shuffle）；4D 输入 r=2"),
    "prelu_kernel": RefEntry(
        lambda x, w: torch.nn.functional.prelu(x, w), _b_prelu,
        note="F.prelu（勿直调 torch._prelu_kernel 私有名）"),
    "renorm": RefEntry(
        lambda x, p, dim, maxnorm: torch.renorm(x, p, dim, maxnorm), _b_renorm,
        note="reduce_dim=1，atol 固定不缩放"),
    "safe_softmax": RefEntry(
        lambda x, dim: torch.softmax(x, dim), _b_safe_softmax,
        reduce_scale_fn=_safe_softmax_scale,
        note="=aten._safe_softmax 的稳定 softmax 语义；atol×末维"),
    "select_scatter": RefEntry(
        lambda x, src, dim, index: torch.select_scatter(x, src, dim, index),
        _b_select_scatter,
        note="2DOnly 口径"),
    "split_with_sizes_copy": RefEntry(
        lambda x, sizes, dim: torch.split_with_sizes(x, list(sizes), dim=dim),
        _b_split_sizes,
        note="值口径同 *_copy；输出 tuple，递归比对"),
    "tensor_split": RefEntry(
        lambda x, sections: torch.tensor_split(x, sections), _b_tensor_split,
        note="输出 tuple，递归比对"),
    "threshold": RefEntry(
        lambda x, threshold, value: torch.nn.functional.threshold(x, threshold, value),
        _b_threshold,
        note="F.threshold（等价 torch.where(x<=3.14, 2.71, x)）"),
    "tile": RefEntry(
        lambda x, dims: torch.tile(x, list(dims)), _b_tile),
    "transpose": RefEntry(
        lambda x, d1, d2: x.transpose(d1, d2), _b_transpose,
        note="=aten.transpose.int"),
    "unfold_copy": RefEntry(
        _unfold_copy_ref, _b_unfold_copy,
        note="硬表 8 组取代表；降级 unfold().clone()"),
    "unfold_view": RefEntry(
        lambda x, dimension, size, step: x.unfold(dimension=dimension, size=size, step=step),
        _b_unfold_view),
    "unsafe_masked_index": RefEntry(
        _unsafe_masked_index_ref, _b_unsafe_masked_index,
        note="Aten 私有名直调（test 原样），降级 where(mask, x[idx], 0)；"
             "value/mask/indices 同 shape（test 语义，2026-09-08 修正）"),
    "unsafe_view": RefEntry(
        lambda x, new_shape: x.view(list(new_shape)), _b_unsafe_view,
        note="=aten._unsafe_view"),
    "unsqueeze": RefEntry(
        lambda x, dim: torch.unsqueeze(x, dim), _b_unsqueeze),

    # ===== B 类：组构（2 条） =====
    "fused_adam": RefEntry(
        _fused_adam_ref, _b_fused_adam, gems_fn=_fused_adam_gems,
        note="B 类组构：抄 test_fused_adam.py 内置 torch_op 语义（返回新 param 不原地改）；"
             "gems 侧列表包装签名；有副作用 → 参照用独立输入实例（builder 每次新建）。"
             "TODO(board): rtol=1.3e-6 需上板实测 fp32 误差分布（CSV special_rules）"),
    "resize_output": RefEntry(
        lambda x, size: x.reshape(list(size)), _b_resize_output, mode=MODE_EXACT,
        note="B 类组构：test 的 torch_op 是 dummy（=gems 自身）；numel 恒等 → 纯形状变换值保留"),

    # ===== C 类：不可构/不适用（4 条） =====
    "assert_async": RefEntry(
        _assert_async_ref, _b_assert_async, mode=MODE_NO_EXCEPTION,
        note="C 类 control_flow：全真输入不抛异常（torch._assert_async 私有名，test 原样）；"
             "异常路径（False 触发）按合同不构造用例"),
    "make_dep_token": RefEntry(
        lambda: _dep_token(), _b_make_dep_token, mode=MODE_NO_EXCEPTION,
        note="C 类 control_plane：真实 op 返回 dep_token 内部对象无数值语义；"
             "test 的 torch_op 是占位 torch.zeros(1)（计时锚点，禁止入比对）；"
             "处置 = 不抛异常且返回结构存在 → correct=True"),
    "dropout": RefEntry(
        lambda *a: None, _b_dropout, gems_fn=_dropout_gems, mode=MODE_STATISTICAL,
        out_adapter=_dropout_adapter,
        note="C 类 stochastic（p=0.5, training=True）：零比例/值域/缩放三统计检查，不走 allclose；"
             "fn 为变参占位（statistical 分支只用 inputs[0] 与 gems_out；compare 会先调 fn，"
             "占位须接受任意元组长度）；builder 带 p=0.5，"
             "gems 侧 dropout(x, 0.5) 返回 (out, mask)，out_adapter 取 [0]（板上实证）"),
    "sparse_semi_structured_mm": RefEntry(
        None, _b_sparse_mm, mode=MODE_UNJUDGEABLE,
        note="C 类：torch CPU 无 2:4 等价，meta 编码语义未定 → correct 恒 None。"
             "TODO(gemm): B 的 GEMM 结果确认 meta 语义后补组合参照 "
             "(mat1 * mask_expand(mat1_meta)) @ mat2 转正"),
}


# ---------------------------------------------------------------------------
# compare：按 CONTRACT 容差判定
# ---------------------------------------------------------------------------

def _dropout_statistical(x, g):
    """dropout 统计校验（p=0.5, training=True）：零比例 / 值域 / 缩放三检查。

    返回 (correct, max_abs_err)。容差用 fp32 合同 (1.3e-6, 1e-4)；
    fp16 扩展时随 CONTRACT 约定调整（注册表不动）。
    """
    import torch
    if not (torch.is_tensor(g) and g.shape == x.shape and g.dtype == x.dtype):
        return False, None
    x32, g32 = x.float(), g.float()
    keep = g32 != 0
    frac = float(keep.float().mean().item())
    if not (0.45 <= frac <= 0.55):          # 检查 1：非零比例 ∈ [0.45, 0.55]
        return False, None
    scaled, _ = (2.0 * x32).reshape(-1).sort()
    vals, _ = g32.reshape(-1)[keep.reshape(-1)].sort()
    if scaled.numel() == 0 or vals.numel() == 0:
        return False, None
    pos = torch.searchsorted(scaled, vals)
    pos_l = (pos - 1).clamp(min=0)
    pos_r = pos.clamp(max=scaled.numel() - 1)
    err = torch.minimum((vals - scaled[pos_l]).abs(), (vals - scaled[pos_r]).abs())
    # 检查 2+3：非零值 ∈ 2x 值域集合（值域）且 ≈ 2×对应元素（缩放），sorted 后合同浮点比对
    rtol, atol = 1.3e-6, 1e-4
    ok = bool((err <= atol + rtol * vals.abs()).all().item())
    return ok, float(err.max().item())


def _match(r, g, rtol, atol):
    """递归比对 tensor/标量/tuple/list。返回 (correct, max_abs_err)。

    correct=None 表示不可比（类型不支持/比较抛异常）。NaN 伪报排除：
    两侧同 NaN 视为相等（rtol=atol=0 走 NaN-aware equal，容差路径 equal_nan=True）。
    """
    import torch
    if torch.is_tensor(r) and torch.is_tensor(g):
        if r.shape != g.shape:
            return False, None
        if not r.dtype.is_floating_point:
            ok = bool(torch.equal(r, g))    # int/bool 域（bitwise_and、compare 类）逐位精确
            return ok, (0.0 if ok else None)
        r32, g32 = r.float(), g.float()
        diff = (r32 - g32).abs()
        if rtol == 0.0 and atol == 0.0:
            same = (r32 == g32) | (r32.isnan() & g32.isnan())
            if bool(same.all().item()):
                return True, 0.0
            masked = torch.where(same, torch.zeros_like(diff),
                                 torch.nan_to_num(diff, nan=float("inf")))
            return False, float(masked.max().item())
        ok = bool(torch.allclose(r32, g32, rtol=rtol, atol=atol, equal_nan=True))
        finite = diff[~diff.isnan()]        # 同 NaN 位置不计入 err（视为相等）
        err = float(finite.max().item()) if finite.numel() else 0.0
        return ok, err
    if isinstance(r, (bool, int, float)) and isinstance(g, (bool, int, float)):
        return bool(r == g), 0.0
    if isinstance(r, (list, tuple)) and isinstance(g, (list, tuple)):
        if len(r) != len(g):
            return False, None
        oks, max_err = [], 0.0
        for rr, gg in zip(r, g):
            ok_r, err_r = _match(rr, gg, rtol, atol)
            if ok_r is None:
                return None, None
            oks.append(ok_r)
            if err_r is not None:
                max_err = max(max_err, err_r)
        return all(oks), max_err
    if r is g:                               # dep_token 等非数值对象：恒等
        return True, 0.0
    try:
        return bool(r == g), 0.0
    except Exception:  # noqa: BLE001
        return None, None


def _infer_float_dtype(*values):
    import torch
    stack = list(values)
    while stack:
        v = stack.pop(0)
        if torch.is_tensor(v):
            if v.dtype.is_floating_point:
                return v.dtype
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
    return None


def compare(op, inputs, gems_out):
    """对 gems 输出做参照判定。返回 {"correct", "max_abs_err", "correct_mode"}（诊断时附加 "error"）。

    - contract：CONTRACT[op] 容差（rtol=atol=0 → torch.equal）；归约缩放类乘 reduce_scale_fn。
    - exact：定义性 torch.equal。
    - no_exception：参照调用不抛异常 + gems 侧有返回（bench_op 拿到 gems_out 即未抛）→ True。
    - statistical：dropout 统计校验。
    - unjudgeable：correct 恒 None。
    参照侧抛异常 → correct=None + error（不误报 incorrect）。op 未注册 → KeyError。
    """
    entry = REFS[op]
    mode = entry.mode
    if mode == MODE_UNJUDGEABLE:
        return {"correct": None, "max_abs_err": None, "correct_mode": mode}
    if mode == MODE_NO_EXCEPTION:
        if entry.fn is not None:
            try:
                entry.fn(*inputs)
            except Exception as exc:  # noqa: BLE001
                return {"correct": None, "max_abs_err": None, "correct_mode": mode,
                        "error": f"ref 调用抛异常: {type(exc).__name__}: {exc}"}
        return {"correct": True, "max_abs_err": None, "correct_mode": mode}
    try:
        ref_out = entry.fn(*inputs)
    except Exception as exc:  # noqa: BLE001
        return {"correct": None, "max_abs_err": None, "correct_mode": mode,
                "error": f"ref 调用抛异常: {type(exc).__name__}: {exc}"}
    if entry.out_adapter is not None:
        try:
            ref_out, gems_out = entry.out_adapter(ref_out, gems_out)
        except Exception as exc:  # noqa: BLE001
            return {"correct": None, "max_abs_err": None, "correct_mode": mode,
                    "error": f"out_adapter 抛异常: {type(exc).__name__}: {exc}"}
    if mode == MODE_STATISTICAL:
        ok, err = _dropout_statistical(inputs[0], gems_out)
        return {"correct": ok, "max_abs_err": err, "correct_mode": mode}
    tol = CONTRACT.get(op)
    if tol is None:
        import torch
        dt = _infer_float_dtype(inputs, ref_out, gems_out)
        tol = (5e-2, 5e-2) if dt == torch.float16 else (5e-3, 5e-3)
    rtol, atol = tol
    if entry.reduce_scale_fn is not None:
        atol = atol * float(entry.reduce_scale_fn(inputs))
    ok, err = _match(ref_out, gems_out, rtol, atol)
    return {"correct": ok, "max_abs_err": err, "correct_mode": mode}


def build_inputs(op, shape=MAIN_SHAPE, dtype=None, device="cpu"):
    """便捷入口：构造 op 的参照输入元组（内部 manual_seed(0)，与挂点同口径）。"""
    import torch
    return REFS[op].input_builder(shape, dtype or torch.float32, device)


# ---------------------------------------------------------------------------
# CLI：--list / --selftest / --selftest-torch
# ---------------------------------------------------------------------------

def _mode_distribution():
    dist = {}
    for e in REFS.values():
        dist[e.mode] = dist.get(e.mode, 0) + 1
    return dist


def _expected_distribution():
    return {MODE_CONTRACT: 37, MODE_EXACT: 2, MODE_NO_EXCEPTION: 2,
            MODE_STATISTICAL: 1, MODE_UNJUDGEABLE: 1}


def _tol_str(op):
    if op not in CONTRACT:
        return "-/- (statistical)"
    rt, at = CONTRACT[op]
    return f"{rt:g}/{at:g}"


def _cmd_list() -> int:
    print(f"fallback_refs: {len(REFS)} ops | CONTRACT source: {_CSV_PATH} "
          f"({'present' if _CSV_PATH.exists() else 'MISSING -> embedded fallback'})")
    for op in CANONICAL_OPS:
        e = REFS[op]
        note = f"  # {e.note}" if e.note else ""
        print(f"{op:34s} {e.mode:14s} rtol/atol={_tol_str(op):16s}{note}")
    print("\nmode distribution:")
    for m, n in sorted(_mode_distribution().items()):
        print(f"  {m:14s} {n}")
    return 0


def _cmd_selftest() -> int:
    """结构自测（不 import torch）：条目齐全性、mode 分布、CONTRACT 编译/读取正确性。"""
    fails = []
    total = 0

    def check(name, cond, detail=""):
        nonlocal total
        total += 1
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            fails.append(name)

    print("[fallback_refs selftest] (torch-free)")
    check("registry 43 ops 齐全且名字精确匹配",
          len(REFS) == 43 and set(REFS) == set(CANONICAL_OPS),
          f"len={len(REFS)}, missing={sorted(set(CANONICAL_OPS) - set(REFS))}, "
          f"extra={sorted(set(REFS) - set(CANONICAL_OPS))}")
    check("每条 input_builder 可调用", all(callable(e.input_builder) for e in REFS.values()))
    check("fn 为 None 的恰为 C 类 dropout/sparse_semi_structured_mm",
          {k for k, e in REFS.items() if e.fn is None} == _FN_NONE_OK,
          f"fn=None: {sorted(k for k, e in REFS.items() if e.fn is None)}")
    check("mode 值全部合法", all(e.mode in _VALID_MODES for e in REFS.values()))
    dist, exp = _mode_distribution(), _expected_distribution()
    check("mode 分布 37/2/2/1/1 (contract/exact/no_exception/statistical/unjudgeable)",
          dist == exp, f"got={dist}")
    check("CONTRACT 覆盖 42 条（dropout 为 statistical 不入表）",
          len(CONTRACT) == 42 and "dropout" not in CONTRACT,
          f"len={len(CONTRACT)}")
    missing = [op for op in CANONICAL_OPS if op != "dropout" and op not in CONTRACT]
    check("CONTRACT 覆盖全部需数值判定的 op", not missing, f"missing={missing}")
    # CSV 编译/读取正确性：独立重读 CSV 对齐 CONTRACT；CSV 缺席时 CONTRACT 须等于内嵌回退表
    if _CSV_PATH.exists():
        csv_vals = {}
        with open(_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                op = (row.get("op") or "").strip()
                if op in CANONICAL_OPS:
                    try:
                        csv_vals[op] = (float(row["rtol"]), float(row["atol"]))
                    except (KeyError, ValueError):
                        continue
        diff = {k: (CONTRACT.get(k), v) for k, v in csv_vals.items() if CONTRACT.get(k) != v}
        check("CONTRACT 与 numeric-contract.csv 逐值一致",
              not diff and len(csv_vals) == 42,
              f"csv_rows={len(csv_vals)}, diff={diff}")
    else:
        check("CSV 缺席 → CONTRACT 等于内嵌回退表（板上白名单场景）",
              CONTRACT == _CONTRACT_FALLBACK)
    for op, want in [("add_relu", (1.3e-6, 1e-4)), ("as_strided_copy", (0.0, 0.0)),
                     ("fused_adam", (1.3e-6, 1e-4)), ("threshold", (1.3e-6, 1e-4)),
                     ("group_norm", (1.3e-6, 1e-4)),
                     ("sparse_semi_structured_mm", (1.3e-6, 1e-4))]:
        check(f"spot 合同 {op}={want}", CONTRACT.get(op) == want, f"got={CONTRACT.get(op)}")
    check("reduce_scale_fn 恰挂在 group_norm / safe_softmax",
          REFS["group_norm"].reduce_scale_fn is not None
          and REFS["safe_softmax"].reduce_scale_fn is not None
          and sum(e.reduce_scale_fn is not None for e in REFS.values()) == 2)
    check("kthvalue out_adapter（只比 values）已注册", REFS["kthvalue"].out_adapter is not None)
    check("fused_adam gems_fn（列表包装闭包）已注册", REFS["fused_adam"].gems_fn is not None)

    status = "PASS" if not fails else f"FAIL ({len(fails)} checks: {fails})"
    print(f"selftest {status} — {total - len(fails)}/{total} checks")
    return 0 if not fails else 1


def _flaggems_ok():
    try:
        import flag_gems  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _cmd_selftest_torch() -> int:
    """torch 依赖自测：全部 input_builder/fn 可执行 + compare 管线（自洽/失配/NaN/统计）。"""
    try:
        import torch
        import torch.nn.functional as F
    except ImportError:
        print("SKIP: 本机无 torch —— 本地 python 与 kope-mem/.venv 均未安装；"
              "请上板跑或用带 torch 的 venv：python board/fallback_refs.py --selftest-torch")
        return 0
    # 注册表 lambda 以模块级名解析 torch/flag_gems（bench_op 挂点注入同款）；
    # CLI 直跑时模块是 __main__，必须自注入，否则 NameError（2026-09-08 板上实证）
    globals()["torch"] = torch
    import fallback_refs as fr  # 以模块身份复用内部件
    if _flaggems_ok():
        import flag_gems
        globals()["flag_gems"] = flag_gems  # _gems_resolver 依赖模块级名

    fails = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            fails.append(name)

    print(f"[fallback_refs selftest-torch] torch={torch.__version__}")

    # 1) 全条目 input_builder + fn 可执行（小 shape 提速；硬表条目自行忽略 shape）
    bad = []
    for op in CANONICAL_OPS:
        e = REFS[op]
        try:
            inputs = e.input_builder((16, 16, 16), torch.float32, "cpu")
            if e.fn is not None:
                e.fn(*inputs)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{op}: {type(exc).__name__}: {exc}")
    check("43 条 input_builder+fn 全部可执行（fn=None 跳过）", not bad, "; ".join(bad[:4]))

    # 2) compare 自洽（gems_out = 独立重算的 ref）→ correct=True
    for op in ("add_relu", "threshold", "greater_equal", "kthvalue", "chunk",
               "fused_adam", "resize_output", "safe_softmax", "tensor_split",
               "group_norm", "jagged_to_padded_dense_forward",
               "functional_sym_constrain_range"):
        e = REFS[op]
        inputs = e.input_builder((16, 16, 16), torch.float32, "cpu")
        src = "ref"
        try:
            if e.gems_fn is not None and _flaggems_ok():
                try:
                    gems = e.gems_fn(*inputs)
                    src = "gems_fn"
                except Exception as exc:  # noqa: BLE001 板上 gems 挂载缺席（如 fused_adam）→ ref 回退
                    gems = e.fn(*inputs)
                    src = f"ref(gems_fn 失败: {type(exc).__name__})"
            else:
                gems = e.fn(*inputs)
        except Exception as exc:  # noqa: BLE001 参照自身板上不可执行（如 dep_token 缺 API）→ FAIL 不崩
            check(f"compare 自洽 {op}", False, f"src={src} {type(exc).__name__}: {exc}")
            continue
        res = compare(op, inputs, gems)
        check(f"compare 自洽 {op}", res["correct"] is True, f"src={src} {res}")

    # 3) 失配检测 → correct=False
    inputs = REFS["add_relu"].input_builder((16, 16, 16), torch.float32, "cpu")
    g = REFS["add_relu"].fn(*inputs) + 1e-3
    res = compare("add_relu", inputs, g)
    check("compare 失配（ref+1e-3）→ False", res["correct"] is False, str(res))

    # 4) NaN 伪报排除：两侧同 NaN 视为相等；异值不可蒙混；容差路径 equal_nan
    a = torch.tensor([1.0, float("nan")])
    ok_same = fr._match(a, a.clone(), 0.0, 0.0)[0]
    ok_diff = fr._match(a, torch.tensor([1.0, 2.0]), 0.0, 0.0)[0]
    ok_tol = fr._match(a, a.clone(), 1.3e-6, 1e-4)[0]
    check("NaN 伪报排除（同 NaN=相等 / 异值=False / 容差路径 equal_nan）",
          ok_same is True and ok_diff is False and ok_tol is True,
          f"same={ok_same} diff={ok_diff} tol={ok_tol}")

    # 5) dropout statistical：换 seed 的真实 dropout → True；全零输出 → False
    x = REFS["dropout"].input_builder((256, 256), torch.float32, "cpu")[0]
    torch.manual_seed(123)
    res = compare("dropout", (x,), F.dropout(x, 0.5, True))
    check("dropout 统计校验（真实 dropout 输出）→ True", res["correct"] is True, str(res))
    res0 = compare("dropout", (x,), torch.zeros_like(x))
    check("dropout 统计校验（全零输出）→ False", res0["correct"] is False, str(res0))

    # 6) no_exception：assert_async / make_dep_token
    res_a = compare("assert_async", REFS["assert_async"].input_builder((16, 16), None, "cpu"), None)
    res_d = compare("make_dep_token", REFS["make_dep_token"].input_builder(), None)
    check("no_exception assert_async → True", res_a["correct"] is True, str(res_a))
    check("no_exception make_dep_token → True（dep_token 构造尽力而为）",
          res_d["correct"] is True or res_d["correct"] is None, str(res_d))

    # 7) unjudgeable：correct 恒 None
    res_s = compare("sparse_semi_structured_mm",
                    REFS["sparse_semi_structured_mm"].input_builder(), None)
    check("unjudgeable sparse_semi_structured_mm → correct=None",
          res_s["correct"] is None, str(res_s))

    status = "PASS" if not fails else f"FAIL ({len(fails)}: {fails})"
    print(f"selftest-torch {status}")
    return 0 if not fails else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="框架兜底层 torch 参照注册表（43 op）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="打印注册表与 mode 分布")
    g.add_argument("--selftest", action="store_true", help="不依赖 torch 的结构自测")
    g.add_argument("--selftest-torch", action="store_true",
                   help="依赖 torch 的构建/比对自测（无 torch 则 SKIP）")
    args = ap.parse_args()
    if args.list:
        sys.exit(_cmd_list())
    if args.selftest:
        sys.exit(_cmd_selftest())
    sys.exit(_cmd_selftest_torch())


if __name__ == "__main__":
    main()
