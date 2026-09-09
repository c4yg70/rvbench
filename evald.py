#!/usr/bin/env python3
"""evald — SpacemiT K3 (RISC-V) 算子代码评估服务器

本地优化好的 FlagGems/Triton 算子代码经 HTTP 提交为评估作业（UUIDv7），
每个作业自包含部署-恢复 site-packages 候选文件（防异步队列下的代码残留）。
作业按 "lane" 字段分三车道，各对应一个可开关的评估器（feature flags，
config.evaluators，至少启用一个）：timing（默认，perf）＝单 worker 串行
执行并持有 /tmp/k3_bench.lock（与手工 SSH 基准互斥，计时语义不变）；
correctness（acc）＝并行 worker 池、不持计时锁、强制 warmup=0/rep=1、
taskset 分核（handoff a100-x100 §3-L2），仅数值精度筛选；func＝功能冒烟
车道（资源口径随 acc，K3_LANE=func），warmup=0/rep=1 强制，跑通（产出
JSON 结果行）即 pass。评估器停用后对应车道请求直接 400。

可选顶层 `perf` 字段是 perf/acc/func 三评估器的资源调度覆盖层（核数/核id/
最大并发/每车道队列深度/omp/执行器/超时），字段本身与其中每一项皆可选；
缺省完全回落 evaluators 与旧扁平键（逐字节兼容），给出时按 perf>evaluators>
旧扁平键>硬缺省优先级归并（见 normalize_perf_resources）。

设计规格：docs/eval-server-design.md；决策：docs/adr/。
仅用 stdlib（板上 FlagGems venv 无 pip，见 ADR-0003）。

用法（板上系统 python3.14，原生 uuid7）::

  python3 evald.py --config /home/c4y/autoke/evald/config.json   # 前台运行
  python3 evald.py --selftest --config ...                       # 自检（不监听端口）
  python3 evald.py --gen-token                                   # 生成随机 token
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

TERMINAL_STATES = {"done", "failed", "timeout", "canceled"}
TAIL_BYTES = 4096

# op 名：允许前导下划线（_reshape_alias），长度上限 48（torch 最长算子名）；
# 字符集仍限 [a-z0-9_]，天然拒绝点/斜杠等路径危险字符
RE_OP = re.compile(r"^[a-z_][a-z0-9_]{0,47}$")
RE_SHAPE = re.compile(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")
# 单次提交的 shape 数上限：与 _parse_json_lines 的 cap 对齐，防止一行作业吃掉
# 整条车道的墙钟预算（多 shape 共享一次 import，故上限不宜大）
MAX_SHAPES = 16
GEMM_OPS = {"mm", "addmm", "bmm"}
OP_DTYPES = {"float32", "float16", "int32", "int64", "bool"}


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t if t is not None else time.time()))


def uuid7() -> str:
    if hasattr(uuid, "uuid7"):  # Python >= 3.14
        return str(uuid.uuid7())
    b = bytearray(os.urandom(16))
    b[0:6] = (time.time_ns() // 1_000_000).to_bytes(6, "big")
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("token", "flag_gems_root", "scripts_root", "run_env"):
        if not cfg.get(key):
            raise SystemExit(f"config 缺少必填项: {key}")
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", 8788)
    cfg.setdefault("data_dir", os.path.dirname(os.path.abspath(path)))
    cfg.setdefault("lock_file", "/tmp/k3_bench.lock")
    cfg.setdefault("queue_max", 64)  # baseline 批量捞回阶段减少客户端 429
    cfg.setdefault("timeout_default_min", 15)
    cfg.setdefault("timeout_max_min", 30)
    cfg.setdefault("body_max_bytes", 8 * 1024 * 1024)
    cfg.setdefault("history_mem", 1000)
    cfg.setdefault("longpoll_max_s", 60)
    cfg.setdefault("executor", "warmfork")  # M3 起默认预热执行器；可回退 "subprocess"
    cfg.setdefault("warmd_path", os.path.join(cfg["data_dir"], "warmd.py"))
    cfg.setdefault("warmd_env", {})
    # L2 correctness 车道（handoff §3-L2）：并行正确性筛选，不持计时锁、不进稳态计时。
    # 回退开关：correctness_workers=0 即回到纯计时车道（无 lane 字段行为与旧版一致）。
    # 以下扁平键是旧版兼容缺省：evaluators.acc 未显式给出 workers/cpus/omp_threads 时回落
    cfg.setdefault("correctness_workers", 3)      # correctness 并行 worker 数；0=关闭车道
    cfg.setdefault("correctness_cpus", "4-6")     # 车道 taskset 核（须为守护 allowed_list 子集：X100 部署 4-6；A100 注册 8-15 后用 12-14，disjoint 会 EINVAL）
    cfg.setdefault("correctness_omp_threads", 1)  # 注入 OMP_NUM_THREADS（与分核对齐，防 3 作业超订）
    cfg.setdefault("correctness_executor", None)  # 车道默认执行器；None=跟随请求/全局 cfg（warmfork）
    normalize_evaluators(cfg)  # 评估器 feature flags（perf/acc/func）归一化 + 启动校验
    normalize_perf_resources(cfg)  # 可选顶层 perf 字段：资源/调度覆盖层归并（缺省即旧行为）
    return cfg


# ---------------------------------------------------------------- 评估器 feature flags

_EVALUATOR_KEYS = {
    "perf": {"enabled", "warmup_times", "repeat_times", "workers", "cpus",
             "omp_threads", "executor", "timeout_min"},
    "acc": {"enabled", "warmup_times", "repeat_times", "workers", "cpus",
            "omp_threads", "executor", "timeout_min", "split_cpus"},
    "func": {"enabled", "warmup_times", "repeat_times", "workers", "cpus",
             "omp_threads", "executor", "timeout_min", "split_cpus"},
}
LANE_EVALUATOR = {"timing": "perf", "correctness": "acc", "func": "func"}
EVALUATOR_LANE = {v: k for k, v in LANE_EVALUATOR.items()}  # perf→timing、acc→correctness…
# 顶层可选 `perf` 字段：perf/acc/func 三评估器的资源/调度覆盖层。字段本身与
# 其中每一项皆可选；缺省完全回落 evaluators/旧扁平键（逐字节兼容）。
_PERF_NAMES = ("perf", "acc", "func")
_PERF_SECTION_KEYS = {"cores", "cpu_ids", "max_concurrency", "max_queue",
                      "omp_threads", "executor", "timeout_min", "split_cpus"}


def normalize_evaluators(cfg):
    """评估器 feature flags（perf/acc/func）归一化与启动校验。

    perf=计时评估器（timing 车道）；acc=数值精度评估器（correctness 车道）；
    func=功能冒烟评估器（func 车道，跑通即 pass）。三者独立开关、至少启用
    一个；缺省推导保持旧版行为（perf 开 / acc 随 correctness_workers / func 关）。
    acc/func 恒定 warmup=0/rep=1——配置显式给了别的值视为配置错误、启动即拒；
    请求体 warmup/rep 也会被覆盖为 0/1 并注记 requested_* 原值。
    归一化结果（含各评估器生效资源）写回 cfg["evaluators"]。
    """
    legacy = {"perf": {"enabled": True},
              "acc": {"enabled": bool(cfg.get("correctness_workers", 3))},
              "func": {"enabled": False}}
    ev = cfg.setdefault("evaluators", legacy)
    if not isinstance(ev, dict) or set(ev) - set(_EVALUATOR_KEYS):
        raise SystemExit(f"config.evaluators 只接受 {sorted(_EVALUATOR_KEYS)}")
    norm = {}
    for name, allowed in _EVALUATOR_KEYS.items():
        sec = ev.get(name) or {}
        if not isinstance(sec, dict) or set(sec) - allowed:
            raise SystemExit(f"evaluators.{name} 只接受键 {sorted(allowed)}")
        norm[name] = dict(sec)
    perf = norm["perf"]
    perf["enabled"] = bool(perf.get("enabled", legacy["perf"]["enabled"]))
    # perf 计时缺省轮数（请求体 warmup/rep 仍可逐作业覆盖）
    perf.setdefault("warmup_times", 25)
    perf.setdefault("repeat_times", 100)
    for key, lo in (("warmup_times", 0), ("repeat_times", 1)):
        v = perf[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= 10000:
            raise SystemExit(f"evaluators.perf.{key} 必须是 {lo}..10000 的整数")
    # perf（timing 车道）资源缺省：单 worker、不钉核、不注 OMP——与旧版逐字节一致；
    # 仅当 evaluators.perf 或顶层 perf 字段显式给出时才改变（见 normalize_perf_resources）
    perf.setdefault("workers", 1)
    perf.setdefault("cpus", None)
    perf.setdefault("omp_threads", None)
    if isinstance(perf["workers"], bool) or not isinstance(perf["workers"], int) \
            or perf["workers"] < 1:
        raise SystemExit("evaluators.perf.workers 必须是 >=1 的整数")
    if perf["cpus"] is not None and (not isinstance(perf["cpus"], str) or not perf["cpus"]):
        raise SystemExit("evaluators.perf.cpus 必须是非空字符串或 null")
    if perf["omp_threads"] is not None and (isinstance(perf["omp_threads"], bool)
            or not isinstance(perf["omp_threads"], int) or perf["omp_threads"] < 1):
        raise SystemExit("evaluators.perf.omp_threads 必须是 >=1 的整数或 null")
    for name in ("acc", "func"):
        sec = norm[name]
        sec["enabled"] = bool(sec.get("enabled", legacy[name]["enabled"]))
        for key, want in (("warmup_times", 0), ("repeat_times", 1)):
            if key in sec and sec[key] != want:
                raise SystemExit(f"evaluators.{name}.{key} 恒为 {want}"
                                 f"（acc/func 不做稳态计时），拒绝 {sec[key]!r}")
        sec["warmup_times"], sec["repeat_times"] = 0, 1
        prev = norm["acc"] if name == "func" else None  # func 资源缺省随 acc 生效值
        sec.setdefault("workers",
                       cfg.get("correctness_workers", 3) if name == "acc" else 2)
        sec.setdefault("cpus",
                       cfg.get("correctness_cpus", "4-6") if prev is None
                       else prev["cpus"])
        sec.setdefault("omp_threads",
                       cfg.get("correctness_omp_threads", 1) if prev is None
                       else prev["omp_threads"])
        if "split_cpus" in sec and not isinstance(sec["split_cpus"], bool):
            raise SystemExit(f"evaluators.{name}.split_cpus 必须是布尔值")
        if not sec["enabled"]:
            continue  # 旧版兼容：correctness_workers=0 推导 acc 关，资源不做约束
        if (isinstance(sec["workers"], bool) or not isinstance(sec["workers"], int)
                or sec["workers"] < 1):
            raise SystemExit(f"evaluators.{name}.workers 必须是 >=1 的整数")
        if not isinstance(sec["cpus"], str) or not sec["cpus"]:
            raise SystemExit(f'evaluators.{name}.cpus 必须是非空字符串（如 "4-7"）')
    # executor / timeout_min 是三评估器共用的可选项（亦可由顶层 perf 字段归并进来）；
    # 此处校验 evaluators 节直接给出的值，perf 字段的值在 normalize_perf_resources 校验
    for name in _PERF_NAMES:
        sec = norm[name]
        if "executor" in sec and sec["executor"] not in ("subprocess", "warmfork"):
            raise SystemExit(f"evaluators.{name}.executor 只能是 subprocess | warmfork")
        if "timeout_min" in sec:
            tm = sec["timeout_min"]
            if isinstance(tm, bool) or not isinstance(tm, (int, float)) \
                    or not (0.1 <= tm <= cfg["timeout_max_min"]):
                raise SystemExit(
                    f"evaluators.{name}.timeout_min 必须在 0.1..{cfg['timeout_max_min']} 分钟之间")
    if not any(norm[name]["enabled"] for name in norm):
        raise SystemExit("config.evaluators: 至少启用一个评估器 (perf/acc/func)")
    if cfg.get("force_lane"):
        if cfg["force_lane"] not in LANE_EVALUATOR:
            raise SystemExit(f"config.force_lane 只能是 {sorted(LANE_EVALUATOR)}")
        if not norm[LANE_EVALUATOR[cfg["force_lane"]]]["enabled"]:
            raise SystemExit(f"config.force_lane={cfg['force_lane']} 对应评估器未启用")
    cfg["evaluators"] = norm
    return cfg


# ---------------------------------------------------------------- perf 资源覆盖层（可选）

def _parse_cpu_spec(spec):
    """把 taskset 风格 CPU 规格（"12-14"/"0,2,4"/"0-3,8"）或 int 列表解析为
    升序去重核 id 列表；非法（空/负数/lo>hi/非数字/非 str|list）返回 None。"""
    if isinstance(spec, list):
        ids = []
        for x in spec:
            if isinstance(x, bool) or not isinstance(x, int) or x < 0:
                return None
            ids.append(x)
        return sorted(set(ids)) or None
    if not isinstance(spec, str) or not spec.strip():
        return None
    ids = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            return None
        if "-" in part:
            lo, _, hi = part.partition("-")
            if not (lo.strip().isdigit() and hi.strip().isdigit()):
                return None
            lo, hi = int(lo), int(hi)
            if lo > hi:
                return None
            ids.extend(range(lo, hi + 1))
        elif part.isdigit():
            ids.append(int(part))
        else:
            return None
    return sorted(set(ids)) or None


def _format_cpu_ids(ids):
    """升序核 id 列表压回紧凑 taskset 字符串（连续段 a-b，孤立 a）。"""
    ids = sorted(set(ids))
    if not ids:
        return ""
    out, start, prev = [], ids[0], ids[0]
    for x in ids[1:]:
        if x == prev + 1:
            prev = x
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = x
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(out)


def _perf_int(name, key, v, lo, hi=None):
    """perf 字段整数校验：非 bool 的 int 且 lo<=v(<=hi)；否则 SystemExit。"""
    if isinstance(v, bool) or not isinstance(v, int) or v < lo or (hi is not None and v > hi):
        rng = f"{lo}..{hi}" if hi is not None else f">={lo}"
        raise SystemExit(f"config.perf.{name}.{key} 必须是 {rng} 的整数")


def normalize_perf_resources(cfg):
    """归并可选顶层 `perf` 字段（perf/acc/func 资源与调度覆盖层）。

    优先级：perf 字段 > evaluators > 旧扁平键（correctness_*）> 硬缺省。
    把 cores/cpu_ids→evaluators.<n>.cpus、max_concurrency→workers、
    omp_threads/executor/timeout_min→同名键、max_queue→cfg["lane_queue_max"][lane]
    归并；下游（lane_resources/Worker/Store/validate_request）只读归并后结构。
    `perf` 的值可为 JSON 对象或其字符串形式（json 字符串），后者自动 json.loads。
    字段缺省时仅初始化 lane_queue_max 为全 None（不设车道级队列上限）。
    cpus 归并后仍由 lane_resources 按 worker index/total 进一步均分（已有机制）。
    """
    # copy-on-write：避免浅拷贝 cfg 时污染父字典（selftest 大量 dict(cfg)）
    lq = dict(cfg.get("lane_queue_max") or {})
    for lane in LANE_EVALUATOR:
        lq.setdefault(lane, None)
    cfg["lane_queue_max"] = lq

    raw = cfg.get("perf")
    if raw is None:
        return cfg
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"config.perf 为字符串时必须是合法 JSON：{exc}")
    if not isinstance(raw, dict):
        raise SystemExit("config.perf 必须是 JSON 对象或其字符串形式")
    if set(raw) - set(_PERF_NAMES):
        raise SystemExit(f"config.perf 只接受键 {list(_PERF_NAMES)}")

    ev = cfg["evaluators"]  # 已由 normalize_evaluators 归一（每评估器含 workers/cpus/…）
    for name in _PERF_NAMES:
        sec = raw.get(name)
        if sec is None:
            continue
        if not isinstance(sec, dict):
            raise SystemExit(f"config.perf.{name} 必须是 JSON 对象")
        if set(sec) - _PERF_SECTION_KEYS:
            raise SystemExit(f"config.perf.{name} 只接受键 {sorted(_PERF_SECTION_KEYS)}")
        tgt, lane = ev[name], EVALUATOR_LANE[name]

        # 核 id / 核数 → taskset cpus（cpu_ids 精确指定；cores 在既有核池上裁剪）
        cpu_ids, cores = sec.get("cpu_ids"), sec.get("cores")
        if cpu_ids is not None:
            ids = _parse_cpu_spec(cpu_ids)
            if ids is None:
                raise SystemExit(
                    f'config.perf.{name}.cpu_ids 非法（用 "12-14"/"0,2,4"/[12,13,14]）：{cpu_ids!r}')
            if cores is not None:
                _perf_int(name, "cores", cores, 1)
                if len(ids) != cores:
                    raise SystemExit(
                        f"config.perf.{name}: cores={cores} 与 cpu_ids 解析出的 {len(ids)} 核不一致")
            tgt["cpus"] = _format_cpu_ids(ids)
        elif cores is not None:
            _perf_int(name, "cores", cores, 1)
            pool = _parse_cpu_spec(tgt.get("cpus"))  # 既有 evaluators/旧扁平键核池
            if pool is None:
                raise SystemExit(
                    f"config.perf.{name}.cores 需配合 cpu_ids，或该评估器已有 cpus 核池可裁剪")
            if cores > len(pool):
                raise SystemExit(
                    f"config.perf.{name}.cores={cores} 超过既有核池 {tgt.get('cpus')!r}（{len(pool)} 核）")
            tgt["cpus"] = _format_cpu_ids(pool[:cores])

        if "max_concurrency" in sec:
            _perf_int(name, "max_concurrency", sec["max_concurrency"], 1)
            tgt["workers"] = sec["max_concurrency"]
        if "omp_threads" in sec:
            _perf_int(name, "omp_threads", sec["omp_threads"], 1)
            tgt["omp_threads"] = sec["omp_threads"]
        if "split_cpus" in sec:
            if not isinstance(sec["split_cpus"], bool):
                raise SystemExit(f"config.perf.{name}.split_cpus 必须是布尔值")
            tgt["split_cpus"] = sec["split_cpus"]
        if "executor" in sec:
            if sec["executor"] not in ("subprocess", "warmfork"):
                raise SystemExit(f"config.perf.{name}.executor 只能是 subprocess | warmfork")
            tgt["executor"] = sec["executor"]
        if "timeout_min" in sec:
            tm = sec["timeout_min"]
            if isinstance(tm, bool) or not isinstance(tm, (int, float)) \
                    or not (0.1 <= tm <= cfg["timeout_max_min"]):
                raise SystemExit(
                    f"config.perf.{name}.timeout_min 必须在 0.1..{cfg['timeout_max_min']} 分钟之间")
            tgt["timeout_min"] = float(tm)
        if "max_queue" in sec:
            _perf_int(name, "max_queue", sec["max_queue"], 1)
            lq[lane] = sec["max_queue"]

    return cfg


def _split_cpus(spec, index, total):
    """把车道 cpus 串（"0-7" / "2-5,8"）均分为 total 份，取第 index 份（连续段）。

    长度不整除时前面的份各多 1 个；CPU 数 < total 等不可切分情形返回 None
    （调用方回落整段共享掩码——共享总比不跑强）。
    """
    cpus = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            cpus.extend(range(int(a), int(b) + 1))
        elif part:
            cpus.append(int(part))
    if total < 1 or len(cpus) < total or not 0 <= index < total:
        return None
    base, extra = divmod(len(cpus), total)
    start = index * base + min(index, extra)
    size = base + (1 if index < extra else 0)
    chunk = cpus[start:start + size]
    out, run_start, prev = [], None, None
    for c in chunk:
        if prev is None:
            run_start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            out.append(str(run_start) if run_start == prev else f"{run_start}-{prev}")
            run_start = prev = c
    out.append(str(run_start) if run_start == prev else f"{run_start}-{prev}")
    return ",".join(out)


def lane_resources(cfg, lane, index=None, total=None):
    """车道生效资源 (cpus, omp_threads)。

    timing 默认 (None, None)（不钉核/不注 OMP，逐字节兼容）；仅当顶层 perf
    字段为 perf 配了 cpu_ids/omp_threads 时才返回其生效值（opt-in 钉核）。
    index/total 成对给出且 total>1 时，把车道 cpus 均分给各 worker（每 worker
    独占一段核，acc/func 并发零争用）；不可切分时回落整段共享。
    """
    sec = cfg["evaluators"][LANE_EVALUATOR[lane]]  # 车道名→评估器名（correctness→acc）
    cpus, omp = sec.get("cpus"), sec.get("omp_threads")
    if cpus and index is not None and total is not None and total > 1 \
            and sec.get("split_cpus", True):   # split_cpus=false → 整段共享（软限制对照）
        cpus = _split_cpus(cpus, index, total) or cpus
    return cpus, omp


# ---------------------------------------------------------------- 白名单解析

def resolve_path(cfg, relpath):
    """把作业里的相对路径解析为板上绝对路径；白名单外返回 None。

    允许：flag_gems/ops/**、flag_gems/runtime/backend/_spacemit/**、
    scripts/bench_op.py、scripts/bench_gemm.py（ADR-0001 的写入边界）。
    """
    if not isinstance(relpath, str) or "\\" in relpath or relpath.startswith("/"):
        return None
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts[0] == "flag_gems" and len(parts) >= 3:
        rest = parts[1:]
        if rest[0] == "ops":
            root = cfg["flag_gems_root"]
        elif rest[:3] == ["runtime", "backend", "_spacemit"]:
            root = cfg["flag_gems_root"]
        else:
            return None
        abs_path = os.path.normpath(os.path.join(root, *rest))
    elif parts[0] == "scripts" and len(parts) == 2 and parts[1] in ("bench_op.py", "bench_gemm.py"):
        abs_path = os.path.normpath(os.path.join(cfg["scripts_root"], parts[1]))
    else:
        return None
    root_norm = os.path.normpath(root if parts[0] == "flag_gems" else cfg["scripts_root"])
    if os.path.commonpath([root_norm, abs_path]) != root_norm:
        return None
    return abs_path


# ---------------------------------------------------------------- 部署-恢复

def deploy_files(cfg, job_dir, files):
    """备份原文件 → rm 断硬链 → 写候选。每写一个文件就更新 manifest，
    部署中途失败时 restore_files 可按已有 manifest 回滚。"""
    backup_root = os.path.join(job_dir, "backup")
    manifest = []
    for ent in files:
        abs_path = resolve_path(cfg, ent["path"])
        if abs_path is None:
            raise ValueError(f"路径不在白名单内: {ent['path']}")
        existed = os.path.lexists(abs_path)
        backup_path = os.path.join(backup_root, ent["path"])
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        created_dirs = _missing_parents(abs_path)
        if existed:
            shutil.copy2(abs_path, backup_path)
            mode = os.stat(abs_path).st_mode & 0o777
            os.remove(abs_path)  # 断 site-packages 硬链
        else:
            mode = 0o644
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(ent["content"])
        os.chmod(abs_path, mode)
        manifest.append({
            "path": ent["path"], "abs": abs_path, "backup": backup_path,
            "existed": existed, "created_dirs": created_dirs,
            "sha256": sha256_hex(ent["content"]),
        })
        with open(os.path.join(job_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def _missing_parents(abs_path):
    """记录部署时新创建的父目录（restore 时清理空目录用）。"""
    missing = []
    d = os.path.dirname(abs_path)
    while d and not os.path.isdir(d):
        missing.append(d)
        d = os.path.dirname(d)
    return missing


def restore_files(job_dir):
    """按 manifest 恢复原文件（新文件则删除候选）；空目录回收。
    manifest 缺失（部署一个文件都没写成）时静默返回。"""
    mp = os.path.join(job_dir, "manifest.json")
    if not os.path.exists(mp):
        return
    with open(mp, encoding="utf-8") as f:
        manifest = json.load(f)
    for ent in manifest:
        abs_path, backup = ent["abs"], ent["backup"]
        if os.path.lexists(abs_path):
            os.remove(abs_path)
        if ent["existed"]:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            shutil.copy2(backup, abs_path)
        for d in sorted(ent["created_dirs"], key=len, reverse=True):
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except OSError:
                pass


# ---------------------------------------------------------------- 请求校验

def validate_request(cfg, body):
    """校验并清洗提交体。返回 (sanitized, files, err)。"""
    if not isinstance(body, dict):
        return None, None, "body 必须是 JSON 对象"
    bench = body.get("bench", "op")
    if bench not in ("op", "gemm"):
        return None, None, "bench 只能是 op | gemm"
    op = body.get("op")
    if not isinstance(op, str) or not RE_OP.match(op):
        return None, None, "op 不合法"
    if bench == "gemm" and op not in GEMM_OPS:
        return None, None, f"gemm 只支持 {sorted(GEMM_OPS)}"
    dtype = body.get("dtype", "float32")
    if dtype not in OP_DTYPES:
        return None, None, f"dtype 只支持 {sorted(OP_DTYPES)}"
    # shape 支持两种形态：字符串（单 case，行为与旧版逐字节一致）或字符串
    # 列表（同 op 多 shape，一次 import 跑完，摊薄 flag_gems/torch 导入成本）
    shape = body.get("shape")
    if isinstance(shape, str):
        multi, shapes = False, [shape]
    elif isinstance(shape, list) and shape:
        multi, shapes = True, list(shape)
    else:
        return None, None, "shape 必须是字符串或非空字符串列表（如 64,512,512）"
    if len(shapes) > MAX_SHAPES:
        return None, None, f"shape 最多 {MAX_SHAPES} 个"
    for s in shapes:
        if not isinstance(s, str) or not RE_SHAPE.match(s):
            return None, None, f"shape 不合法（如 64,512,512）: {s!r}"
    if len(set(shapes)) != len(shapes):
        return None, None, "shape 列表内不可重复"
    perf_cfg = cfg.get("evaluators", {}).get("perf", {})
    warmup = body.get("warmup", perf_cfg.get("warmup_times", 25))
    rep = body.get("rep", perf_cfg.get("repeat_times", 100))
    if not isinstance(warmup, int) or not isinstance(rep, int) or not (0 <= warmup <= 10000 and 1 <= rep <= 10000):
        return None, None, "warmup/rep 必须是 0..10000 的整数"
    batch = body.get("batch", 8)
    if not isinstance(batch, int) or not (1 <= batch <= 4096):
        return None, None, "batch 必须是 1..4096 的整数"
    restore = body.get("restore", True)
    if not isinstance(restore, bool):
        return None, None, "restore 必须是 bool"
    # 车道级默认超时（perf 字段/evaluators.<n>.timeout_min）优先于全局 timeout_default_min；
    # lane 非法时此处取不到覆盖、随后 lane 校验会拒绝，故用 raw lane 查表是安全的
    _ev_name = LANE_EVALUATOR.get(body.get("lane", "timing"))
    _lane_to = cfg["evaluators"].get(_ev_name, {}).get("timeout_min") if _ev_name else None
    timeout_min = body.get("timeout_min",
                           _lane_to if _lane_to is not None else cfg["timeout_default_min"])
    # 多 shape 共用一份墙钟预算（整批 timeout，不是每 case），上限按 case 数放宽
    timeout_max = cfg["timeout_max_min"] * (len(shapes) if multi else 1)
    if not isinstance(timeout_min, (int, float)) or not (0.1 <= timeout_min <= timeout_max):
        return None, None, f"timeout_min 必须在 0.1..{timeout_max} 分钟之间（整批墙钟）"
    executor = body.get("executor")
    if executor is not None and executor not in ("subprocess", "warmfork"):
        return None, None, "executor 只能是 subprocess | warmfork"
    lane = body.get("lane", "timing")
    if lane not in LANE_EVALUATOR:
        return None, None, f"lane 只能是 {' | '.join(LANE_EVALUATOR)}"
    # feature flag 门禁：对应评估器停用的车道直接拒绝（否则作业无人消费只能超时）；
    # 先门禁后 force_lane 改写——不该来的车道显式拒绝，而非静默改道。
    # K1 池（board-pool.json must_disable）：force_lane 服务端锁死车道，客户端提交
    # 任何 lane/缺省都被强制改写；不配置此键则无改写
    if not cfg.get("evaluators", {}).get(LANE_EVALUATOR[lane], {}).get("enabled", True):
        return None, None, (f"评估器 {LANE_EVALUATOR[lane]} 未启用"
                            f"（config.evaluators.{LANE_EVALUATOR[lane]}.enabled=false）")
    if cfg.get("force_lane"):
        lane = cfg["force_lane"]
    files = body.get("files", [])
    if not isinstance(files, list) or len(files) > 32:
        return None, None, "files 最多 32 个"
    seen = set()
    for ent in files:
        if not isinstance(ent, dict) or not isinstance(ent.get("path"), str) or not isinstance(ent.get("content"), str):
            return None, None, "files[].path/content 必须是字符串"
        if ent["path"] in seen:
            return None, None, f"files 内路径重复: {ent['path']}"
        seen.add(ent["path"])
        if resolve_path(cfg, ent["path"]) is None:
            return None, None, f"路径不在白名单内: {ent['path']}"
    sanitized = {
        "op": op, "shape": shape, "dtype": dtype, "warmup": warmup, "rep": rep,
        "bench": bench, "restore": restore, "timeout_min": timeout_min,
        "executor": executor,
        "files": [{"path": e["path"], "sha256": sha256_hex(e["content"]),
                   "bytes": len(e["content"].encode("utf-8"))} for e in files],
    }
    # 向后兼容：body 无 lane 字段时 sanitized 不带 lane 键（request 回显/落盘逐字节不变）；
    # force_lane 生效时必须显式带 lane 键，否则执行层按缺省 timing 处理
    if "lane" in body or cfg.get("force_lane"):
        sanitized["lane"] = lane
    if lane in ("correctness", "func"):
        # 车道语义：acc 只筛数值精度（handoff §3-L2）、func 只验功能冒烟——都
        # 不做稳态计时，恒定覆盖轮数并注记原值。bench_op 直调层 do_bench 的
        # warmup/rep 是时间预算（n=max(1,...)），0/1 安全；正确性比较发生在
        # do_bench 之外，不受影响
        sanitized["warmup"], sanitized["rep"] = 0, 1
        sanitized["requested_warmup"], sanitized["requested_rep"] = warmup, rep
    if bench == "gemm":
        sanitized["batch"] = batch
    return sanitized, files, None


# ---------------------------------------------------------------- 遥测基础设施

# 固定 bucket（写死，不做动态）：进程内语义，evald 重启清零，不从 spool 重建。
# 与 submissions_total / jobs_total 的持久语义严格区分——后者扫 spool 衔接历史。
HIST_BUCKETS = {
    "queue_wait": (1, 5, 15, 30, 60, 120, 300, 600),
    "job_duration": (5, 15, 30, 60, 90, 120, 300, 600),
    "psr": (1, 2, 4, 8, 16),
}
HIST_METRIC = {
    "queue_wait": "evald_queue_wait_seconds",
    "job_duration": "evald_job_duration_seconds",
    "psr": "evald_psr",
}


def _fmt_le(x):
    """bucket 上界渲染：整数不带小数点，保持与 Prometheus 惯例一致。"""
    return str(int(x)) if float(x).is_integer() else ("%g" % x)


class Histogram:
    """进程内 histogram（重启清零）。le 语义为「≤ 该上界的观测数」。"""

    def __init__(self, kind):
        self.kind = kind
        self.buckets = HIST_BUCKETS[kind]
        self.counts = [0] * len(self.buckets)
        self.count = 0
        self.sum = 0.0

    def observe(self, v):
        if v is None:
            return
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if v != v or v in (float("inf"), float("-inf")):  # NaN/Inf 不入桶
            return
        self.count += 1
        self.sum += v
        for i, b in enumerate(self.buckets):
            if v <= b:
                self.counts[i] += 1

    def as_tuple(self):
        return (self.count, self.sum, list(self.counts))


_RE_JOB_PATH = re.compile(r"^/jobs/[^/?]+")


def normalize_path(p):
    """HTTP 指标的 path label：把 /jobs/<uuid> 归一为 /jobs/{id}。

    基数控制：作业 id / 算子名绝不进 label（237 算子 × 状态 = 序列爆炸）。
    """
    p = (p or "/").split("?")[0]
    m = _RE_JOB_PATH.match(p)
    if m:
        return "/jobs/{id}" + p[m.end():]
    return p or "/"


# ---------------------------------------------------------------- 作业存储

class Job:
    def __init__(self, job_id, seq, request, extra=None):
        self.job_id = job_id
        self.seq = seq
        self.state = "queued"
        self.request = request
        # result_extra（board-pool must_enable）：随响应与落盘记录附加的固定标记
        self.extra = dict(extra) if extra else None
        self.result = None
        self.kope_mem = None
        self.error = None
        self.stdout = ""
        self.stderr = ""
        self.submitted_at = time.time()
        self.started_at = None
        self.finished_at = None

    def to_json(self, full=True):
        o = {
            "job_id": self.job_id, "seq": self.seq, "state": self.state,
            "submitted_at": now_iso(self.submitted_at),
            "started_at": now_iso(self.started_at) if self.started_at else None,
            "finished_at": now_iso(self.finished_at) if self.finished_at else None,
            "queue_wait_s": round(self.started_at - self.submitted_at, 1) if self.started_at else None,
            "duration_s": round(self.finished_at - self.started_at, 1)
            if self.started_at and self.finished_at else None,
            "request": self.request, "result": self.result, "kope_mem": self.kope_mem,
            "error": self.error,
        }
        if getattr(self, "extra", None):
            o.update(self.extra)
        if full:
            o["logs"] = {
                "stdout_tail": self.stdout[-TAIL_BYTES:],
                "stderr_tail": self.stderr[-TAIL_BYTES:],
            }
        return o


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs_dir = os.path.join(cfg["data_dir"], "jobs")
        self.jsonl_path = os.path.join(cfg["data_dir"], "jobs.jsonl")
        os.makedirs(self.jobs_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.jobs = {}
        self.order = deque()          # seq 升序的 job_id（含历史）
        # 按车道分列的队列；queue_max 对全部车道总和生效（L2 提速不放大排队深度）
        self.queues = {"timing": deque(), "correctness": deque(), "func": deque()}
        self.events = {}
        self.seq = 0
        # /metrics 聚合计数器（持久语义：初值由 _init_metrics 扫 spool 衔接历史，
        # 之后由 submit()/finish() 维护；与 textfile 探针的 spool 扫描口径一致）
        self.m_submissions = 0
        self.m_jobs = {"done": 0, "failed": 0, "timeout": 0}
        self.m_last_sub_ts = 0.0
        self.m_last_comp_ts = 0.0
        self.m_last_job = None          # (op, state) —— 只暴露最新一条
        # 进程内遥测（重启清零；与上面 spool 持久计数的语义严格区分，见 §4.3）
        self.m_hist = {"queue_wait": {}, "job_duration": {}, "psr": {}}
        self.m_completed = {}           # {lane: {state: n}}
        self.m_fallback = {}            # warmd→subprocess 回退 {reason: n}
        self.m_restore_fail = 0         # restore 失败（P0：实验环境 §4.6 要 HALT）
        self.m_http = {}                # {(path, status): n}
        self.m_longpoll = 0             # 长轮询挂起数
        # 最近 3 个已完成作业（idx0=最新），元素 = (op, state, dur, wait, fin_epoch)。
        # 与 m_last_job 同源语义：重启由 _init_metrics 扫 spool 重建，之后 finish() 维护。
        self.m_recent = deque(maxlen=3)
        self._load_history()
        self._sweep_stale()
        self._init_metrics()

    def _load_history(self):
        if not os.path.exists(self.jsonl_path):
            return
        with open(self.jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()[-self.cfg["history_mem"]:]
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            job = Job(rec["job_id"], rec["seq"], rec.get("request", {}))
            job.state = rec["state"]
            job.result = rec.get("result")
            job.kope_mem = rec.get("kope_mem")
            job.error = rec.get("error")
            job.submitted_at = rec.get("submitted_epoch", 0)
            job.started_at = rec.get("started_epoch")
            job.finished_at = rec.get("finished_epoch")
            self.jobs[job.job_id] = job
            self.order.append(job.job_id)
            self.seq = max(self.seq, job.seq)

    def _sweep_stale(self):
        """上次服务器重启时被打断的作业（有目录无终态记录）标记 failed。"""
        for name in os.listdir(self.jobs_dir):
            jid = name
            d = os.path.join(self.jobs_dir, name)
            if not os.path.isdir(d) or jid in self.jobs:
                continue
            job = Job(jid, self.seq + 1, {})
            job.state = "failed"
            job.error = "server restarted during job"
            job.finished_at = time.time()
            job.started_at = job.submitted_at
            with self.lock:
                self.jobs[jid] = job
                self.order.append(jid)
                self.seq += 1
            self._persist(job)

    def _init_metrics(self):
        """/metrics 计数器初值：扫一遍 spool（jobs/ 永不清理），重启无缝衔接。

        目录数 → submissions_total；result.json 的 state → jobs_total；
        最新 result.json mtime → last_completion（并取其 op/state → last_job_info）；
        最新 request.json mtime → last_submission。与 textfile 探针的 spool
        扫描口径一致，两者可互相对账。
        """
        submissions = 0
        last_sub_ts = 0.0
        last_key, last_info = (0.0, ""), None
        recent_cands = []               # (sort_key, op, state, dur, wait, fin_epoch)
        try:
            names = os.listdir(self.jobs_dir)
        except OSError:
            names = []
        for name in names:
            d = os.path.join(self.jobs_dir, name)
            if not os.path.isdir(d):
                continue
            submissions += 1
            try:
                last_sub_ts = max(last_sub_ts,
                                  os.stat(os.path.join(d, "request.json")).st_mtime)
            except OSError:
                pass
            rp = os.path.join(d, "result.json")
            try:
                comp_ts = os.stat(rp).st_mtime
                with open(rp, encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            state = rec.get("state")
            if state in self.m_jobs:
                self.m_jobs[state] += 1
            # 主序 mtime；旧文件系统秒级粒度下按 ISO finished_at tie-break
            key = (comp_ts, rec.get("finished_at") or "")
            if key > last_key:
                last_key = key
                req = rec.get("request") or {}
                last_info = (req.get("op") or "unknown", state or "unknown")
            # 最近 3 条已完成作业（重建面板 K3 卡「最近 3 个作业」）：与 last_job_info
            # 同源、同排序键；duration_s 缺失（未真正执行）的跳过。fin 用 result.json
            # mtime（≈完成时刻 epoch），与 finish() 路径的 job.finished_at 毫秒级一致。
            if state in ("done", "failed", "timeout") and rec.get("duration_s") is not None:
                rq = rec.get("request") or {}
                recent_cands.append(
                    (key, rq.get("op") or "unknown", state,
                     rec.get("duration_s"), rec.get("queue_wait_s"), comp_ts))
        recent_cands.sort(key=lambda t: t[0], reverse=True)
        with self.lock:
            self.m_submissions = submissions
            self.m_last_sub_ts = last_sub_ts
            self.m_last_comp_ts = last_key[0]
            self.m_last_job = last_info
            self.m_recent = deque(
                [(op, st, dur, wait, fin)
                 for _k, op, st, dur, wait, fin in recent_cands[:3]], maxlen=3)

    def submit(self, sanitized, payload):
        with self.cond:
            lane = sanitized.get("lane", "timing")
            # 全局 queue_max（跨车道总和）+ 可选车道级 max_queue（perf 字段）双闸；
            # 车道级未配置（None）时只受全局闸约束（逐字节兼容旧行为）
            if sum(len(q) for q in self.queues.values()) >= self.cfg["queue_max"]:
                raise QueueFullError()
            lqmax = self.cfg.get("lane_queue_max", {}).get(lane)
            if lqmax is not None and len(self.queues[lane]) >= lqmax:
                raise QueueFullError()
            self.seq += 1
            job = Job(uuid7(), self.seq, sanitized, extra=self.cfg.get("result_extra"))
            self.jobs[job.job_id] = job
            self.order.append(job.job_id)
            # 完整 payload（含代码内容）先落作业目录再入队，避免与 worker 竞态
            d = self.job_dir(job.job_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "request.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            self.queues[lane].append(job.job_id)
            self.m_submissions += 1
            self.m_last_sub_ts = job.submitted_at
            # notify_all：waiter 分属两车道，单 notify 可能被错误车道的 waiter
            # 消费掉（其队列仍空 → 回去睡），作业滞留到下一次提交才被捡起
            self.cond.notify_all()
            return job

    def next_job(self, lane="timing"):
        """取本车道队头；其他车道提交会唤醒本等待（while 循环回守空队列）。"""
        with self.cond:
            while not self.queues[lane]:
                self.cond.wait()
            return self.jobs[self.queues[lane].popleft()]

    def position(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            q = self.queues[job.request.get("lane", "timing")]
            try:
                return list(q).index(job_id) + 1
            except ValueError:
                return None

    def cancel(self, job_id):
        with self.cond:
            job = self.jobs.get(job_id)
            if job is None or job.state != "queued":
                return False
            self.queues[job.request.get("lane", "timing")].remove(job_id)
            job.state = "canceled"
            job.finished_at = time.time()
            self._persist(job)
            self._fire_event(job_id)
            return True

    def lane_stats(self):
        """healthz 用：每车道 queued/running 计数。"""
        with self.lock:
            return {lane: {"queued": len(self.queues[lane]),
                           "running": sum(1 for j in self.jobs.values()
                                          if j.state == "running" and
                                          j.request.get("lane", "timing") == lane)}
                    for lane in self.queues}

    # -- 进程内遥测埋点（全部在 self.lock 下；RLock 可重入，被 finish/observe 复用）

    def observe(self, kind, lane, value):
        """histogram 埋点。进程内语义，重启清零。"""
        with self.lock:
            h = self.m_hist[kind].get(lane)
            if h is None:
                h = self.m_hist[kind][lane] = Histogram(kind)
            h.observe(value)

    def count_completed(self, lane, state):
        with self.lock:
            d = self.m_completed.setdefault(lane, {})
            d[state] = d.get(state, 0) + 1

    def count_fallback(self, reason):
        with self.lock:
            self.m_fallback[reason] = self.m_fallback.get(reason, 0) + 1

    def count_restore_fail(self):
        with self.lock:
            self.m_restore_fail += 1

    def count_http(self, path, code):
        with self.lock:
            k = (path, code)
            self.m_http[k] = self.m_http.get(k, 0) + 1

    def bump_longpoll(self, delta):
        with self.lock:
            self.m_longpoll += delta

    def oldest_queued_age(self, lane):
        """队头作业已等待秒数（O(1)，不扫 spool）；空队列返回 None。"""
        with self.lock:
            q = self.queues[lane]
            if not q:
                return None
            j = self.jobs.get(q[0])
            return (time.time() - j.submitted_at) if j else None

    def metrics_snapshot(self):
        """/metrics 用：聚合计数快照（lane 计数与 lane_stats 同源、同一把锁）。

        渲染必须 O(1)：不扫 spool。spool 规模用运行期计数器 m_submissions 表示。
        """
        with self.lock:
            return {"lanes": self.lane_stats(),
                    "submissions": self.m_submissions,
                    "jobs": dict(self.m_jobs),
                    "last_sub_ts": self.m_last_sub_ts,
                    "last_comp_ts": self.m_last_comp_ts,
                    "last_job": self.m_last_job,
                    "recent": list(self.m_recent),
                    "hist": {k: {lane: h.as_tuple() for lane, h in d.items()}
                             for k, d in self.m_hist.items()},
                    "completed": {lane: dict(d)
                                  for lane, d in self.m_completed.items()},
                    "fallback": dict(self.m_fallback),
                    "restore_fail": self.m_restore_fail,
                    "http": dict(self.m_http),
                    "longpoll": self.m_longpoll,
                    "oldest_age": {lane: self.oldest_queued_age(lane)
                                   for lane in self.queues}}

    def job_dir(self, job_id):
        return os.path.join(self.jobs_dir, job_id)

    def event_for(self, job_id):
        with self.lock:
            if job_id not in self.events:
                self.events[job_id] = threading.Event()
            return self.events[job_id]

    def _fire_event(self, job_id):
        with self.lock:
            evt = self.events.get(job_id)
        if evt:
            evt.set()

    def finish(self, job):
        with self.lock:
            job.finished_at = time.time()
            if job.state in self.m_jobs:    # 终态累计（canceled 不经此处）
                self.m_jobs[job.state] += 1
            self.m_last_comp_ts = job.finished_at
            self.m_last_job = (job.request.get("op") or "unknown", job.state)
            # 最近 3 条已完成作业（idx0=最新）：deque(maxlen=3) 自动淘汰最旧，O(1)
            dur = (job.finished_at - job.started_at) if job.started_at else None
            wait = (job.started_at - job.submitted_at) if job.started_at else None
            self.m_recent.appendleft(
                (job.request.get("op") or "unknown", job.state,
                 dur, wait, job.finished_at))
            # 遥测：按 lane/state 的终态计数（rate 友好）+ 执行时长 + PSR
            lane = job.request.get("lane", "timing")
            self.count_completed(lane, job.state)
            if job.started_at:
                self.observe("job_duration", lane, job.finished_at - job.started_at)
            psr = None
            for src in (job.result, job.kope_mem):
                if isinstance(src, dict) and src.get("psr") is not None:
                    psr = src["psr"]
                    break
            if psr is not None:
                self.observe("psr", lane, psr)
            self._persist(job)
        self._fire_event(job.job_id)
        d = self.job_dir(job.job_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as f:
            json.dump(job.to_json(full=True), f, ensure_ascii=False, indent=1)
        with open(os.path.join(d, "stdout.log"), "w", encoding="utf-8") as f:
            f.write(job.stdout)
        with open(os.path.join(d, "stderr.log"), "w", encoding="utf-8") as f:
            f.write(job.stderr)

    def _persist(self, job):
        rec = job.to_json(full=False)
        rec["submitted_epoch"] = job.submitted_at
        rec["started_epoch"] = job.started_at
        rec["finished_epoch"] = job.finished_at
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def history(self, limit=50, since=None):
        with self.lock:
            since_seq = -1
            if since:
                sj = self.jobs.get(since)
                if sj is None:
                    return None
                since_seq = sj.seq
            out = []
            for jid in self.order:
                j = self.jobs[jid]
                if j.seq > since_seq:
                    out.append(j)
                    if len(out) >= limit:
                        break
            return out


class QueueFullError(Exception):
    pass


# ---------------------------------------------------------------- 执行器（v1 子进程）

class SubprocessExecutor:
    def __init__(self, cfg, lane="timing", index=None, total=None):
        self.cfg = cfg
        self.lane = lane
        self.index, self.total = index, total   # 车道 worker 分核参数（total>1 才切分）

    def _taskset_argv(self):
        """非 timing 车道把 bench 进程钉到本 worker 的核段；timing 车道返回 []（不改变任何行为）。"""
        cpus, _ = lane_resources(self.cfg, self.lane, self.index, self.total)
        if cpus and shutil.which("taskset"):
            return ["taskset", "-c", str(cpus)]
        return []

    def _lane_prefix(self):
        """注入车道环境：K3_LANE（非 timing）与 OMP_NUM_THREADS（按车道配置）。
        timing 默认两者皆无（逐字节兼容）；仅当 perf 字段为 perf 配了 omp_threads
        时才给 timing 注入 OMP（K3_LANE 始终不注入 timing，避免 bench 误判车道）。"""
        exports = []
        if self.lane != "timing":
            exports.append(f"K3_LANE={self.lane}")
        _, omp = lane_resources(self.cfg, self.lane)
        if omp:
            exports.append(f"OMP_NUM_THREADS={omp}")
        return f"export {' '.join(exports)} && " if exports else ""

    def build_command(self, req):
        # 所有参数均已通过 validate_request 的正则/枚举校验，无注入面
        if req["bench"] == "gemm":
            cmd = (f"python {self.cfg['scripts_root']}/bench_gemm.py --op {req['op']} "
                   f"--dtype {req['dtype']} --shapes {req['shape']}")
            if req["op"] == "bmm":
                cmd += f" --batch {req['batch']}"
        else:
            # 多 shape：这是 warmd 故障时的降级路径，用 && 串起来保证每个 shape
            # 都出结果（不追求摊薄 import；摊薄由 warmd 主路径的进程内循环负责）
            base = (f"python {self.cfg['scripts_root']}/bench_op.py --op {req['op']} "
                    f"--shape %s --dtype {req['dtype']} "
                    f"--warmup {req['warmup']} --rep {req['rep']}")
            shapes = req["shape"]
            cmd = " && ".join(base % s for s in
                              (shapes if isinstance(shapes, list) else [shapes]))
        return f"source {self.cfg['run_env']} && {self._lane_prefix()}{cmd}"

    def run(self, job):
        """返回 (state, result, kope_mem, error)。超时杀进程组。"""
        req = job.request
        timeout_s = req["timeout_min"] * 60
        bash_cmd = self.cfg.get("bench_command_override") or self.build_command(req)
        popen_kwargs = {}
        _, omp = lane_resources(self.cfg, self.lane)
        if self.lane != "timing" or omp:
            # 覆盖 bench_command_override（selftest 钩子）时环境也必须到位；
            # bash 内 export 在 source 之后兜底，防 run_env 再覆盖。
            # timing 默认不进此分支（无 K3_LANE/OMP）；配了 perf.omp_threads 才注入 OMP
            env = dict(os.environ)
            if self.lane != "timing":
                env["K3_LANE"] = self.lane
            if omp:
                env["OMP_NUM_THREADS"] = str(omp)
            popen_kwargs["env"] = env
        proc = subprocess.Popen(
            [*self._taskset_argv(), "bash", "-c", bash_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, **popen_kwargs)
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()
            job.stdout, job.stderr = out or "", err or ""
            return "timeout", None, None, f"exceeded {req['timeout_min']} min"
        job.stdout, job.stderr = out or "", err or ""
        if proc.returncode != 0:
            return "failed", None, None, f"bench rc={proc.returncode}"
        rows = _parse_json_lines(out)
        if not rows:
            return "failed", None, None, "no JSON line in stdout"
        result, kope_mem = _map_rows(req["bench"], rows, req.get("shape"))
        return "done", result, kope_mem, None


def _map_rows(bench, rows, shapes=None):
    """把 bench 输出的 JSON 行映射为 (result, kope_mem)。

    多 shape 提交（shapes 为长度 >1 的列表）时按提交顺序给每行标注 shape，
    包成 result.cases[]；**顶层不放 speedup 标量**——顶层放一个会让只看
    speedup 的老驱动静默按第一个 shape 判定，静默错判比显式报错危险得多。
    """
    if bench == "gemm":
        ok = all(r.get("correct") for r in rows)
        spd = rows[0].get("speedup")
        result = rows[0] if len(rows) == 1 else {"metric": "gflops", "rows": rows}
        if isinstance(result, dict) and "metric" not in result:
            result = dict(result, metric="gflops")
        return result, {"speedup": spd, "pass_rate": 1.0 if ok else 0.0}
    if isinstance(shapes, list) and len(shapes) > 1:
        cases = []
        for i, row in enumerate(rows):
            c = dict(row)
            # 按提交顺序标注 shape，一律以提交的字符串为准：真实 bench_op.py 会把
            # shape 解析成 list（如 [64,512,512]），而结果契约（设计 §1.5）与驱动
            # eval_pipeline 都按提交原样的字符串对齐，故覆盖 bench 行的解析形态。
            if i < len(shapes):
                c["shape"] = shapes[i]
            cases.append(c)
        ok = [bool(c.get("correct")) for c in cases]
        result = {"cases": cases, "shapes": list(shapes),
                  "cases_expected": len(shapes), "cases_returned": len(rows)}
        if len(rows) != len(shapes):
            result["partial"] = True
        return result, {"pass_rate": (sum(ok) / len(ok)) if ok else 0.0}
    row = rows[-1]
    return row, {"speedup": row.get("speedup"),
                 "pass_rate": 1.0 if row.get("correct") else 0.0}


def _parse_json_lines(text):
    rows = []
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
            if len(rows) >= 16:
                break
    rows.reverse()
    return rows


# ---------------------------------------------------------------- 执行器（M3 预热 fork）

class WarmForkExecutor:
    """warmd 常驻预热守护 + 每作业 fork 子进程（ADR-0004）。

    父进程只预热 torch+triton（与候选代码无关的不变量）；子进程全新
    import flag_gems/bench 脚本——候选代码永远从磁盘已部署文件加载。
    守护异常（起不来/EOF/协议错）时本作业自动回退 SubprocessExecutor。
    """

    def __init__(self, cfg, lane="timing", store=None, index=None, total=None):
        self.cfg = cfg
        self.lane = lane
        self.store = store          # 仅用于回退埋点（selftest 可传 None）
        self.index, self.total = index, total   # 车道 worker 分核参数（total>1 才切分）
        self.proc = None
        self.stderr_tail = []
        self.lock = threading.Lock()

    def _drain_stderr(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                self.stderr_tail.append(line)
                if len(self.stderr_tail) > 200:
                    del self.stderr_tail[:100]
        except (OSError, ValueError):
            pass

    def _spawn(self):
        env = dict(os.environ)
        env["WARMD_SCRIPTS_ROOT"] = self.cfg["scripts_root"]
        env.update(self.cfg.get("warmd_env", {}))
        cpus, omp = lane_resources(self.cfg, self.lane, self.index, self.total)
        # 车道环境随守护 env 被 fork 子进程继承（warmd 不改语义）；守护整体
        # taskset → 每个作业子进程天然钉在车道核。timing 默认无 K3_LANE/OMP/钉核；
        # 仅当 perf 字段为 perf 配了 cpu_ids/omp_threads 时才生效（opt-in）
        if self.lane != "timing":
            env["K3_LANE"] = self.lane
        if omp:
            env["OMP_NUM_THREADS"] = str(omp)
        cmd = f"source {self.cfg['run_env']} && exec python {self.cfg['warmd_path']}"
        argv = ["bash", "-c", cmd]
        if cpus and shutil.which("taskset"):
            argv = ["taskset", "-c", str(cpus)] + argv
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
            start_new_session=True)  # 独立进程组：卡死时可 killpg 整树
        self.stderr_tail = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        line = self._readline(120)  # 预热 torch+triton 上限
        try:
            if line != "" and line and json.loads(line).get("ready"):
                return
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"warmd not ready: {line!r} {''.join(self.stderr_tail[-5:])[-300:]}")

    def _readline(self, timeout_s):
        """限期读一行；None=超时，''=EOF，否则为行内容。"""
        deadline = time.time() + timeout_s
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            r, _, _ = select.select([self.proc.stdout], [], [], min(remaining, 1.0))
            if r:
                line = self.proc.stdout.readline()
                return line if line else ""

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
        self.proc = None

    def run(self, job):
        req = job.request
        timeout_s = req["timeout_min"] * 60
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._spawn()
                self.proc.stdin.write(json.dumps({
                    "id": job.job_id, "timeout_s": timeout_s,
                    "req": {k: req[k] for k in ("bench", "op", "shape", "dtype",
                                                "warmup", "rep", "batch") if k in req},
                }) + "\n")
                self.proc.stdin.flush()
                line = self._readline(timeout_s + 30)
                if not line:
                    self._kill()
                    raise RuntimeError(
                        f"warmd no response: {'EOF' if line == '' else 'deadline'}")
                resp = json.loads(line)
            except (RuntimeError, OSError, ValueError) as exc:
                self._kill()
                job.stderr = (job.stderr or "") + f"\n[warmd 故障，本作业回退 subprocess: {exc}]"
                # 遥测：回退此前是隐形故障（零信号），现按异常类型计数
                if self.store:
                    self.store.count_fallback(type(exc).__name__)
                return SubprocessExecutor(self.cfg, self.lane,
                                          self.index, self.total).run(job)
        job.stdout = resp.get("stdout") or ""
        job.stderr = (job.stderr or "") + (resp.get("stderr") or "")
        state = resp.get("state", "failed")
        result = kope_mem = None
        error = resp.get("error")
        if state == "done":
            rows = resp.get("result") or []
            if rows:
                result, kope_mem = _map_rows(req["bench"], rows, req.get("shape"))
            else:
                state, error = "failed", "warmd done but empty rows"
        return state, result, kope_mem, error


# ---------------------------------------------------------------- worker

class Worker(threading.Thread):
    def __init__(self, cfg, store, lane="timing", index=0, total=None):
        super().__init__(daemon=True, name="evald-worker" if lane == "timing"
                         else f"evald-worker-{lane}-{index}")
        self.cfg = cfg
        self.store = store
        self.lane = lane
        self.index, self.total = index, total   # 车道 worker 分核参数（total>1 才切分）
        self.executors = {"subprocess": SubprocessExecutor(cfg, lane=lane,
                                                           index=index, total=total),
                          "warmfork": WarmForkExecutor(cfg, lane=lane, store=store,
                                                       index=index, total=total)}

    def _executor_for(self, request):
        # 优先级：请求体 executor > 车道级（perf 字段/evaluators.<n>.executor）
        # > 旧扁平键 correctness_executor（仅 correctness）> 全局 executor
        name = request.get("executor")
        if not name:
            ev_name = LANE_EVALUATOR.get(self.lane)
            name = self.cfg["evaluators"].get(ev_name, {}).get("executor") if ev_name else None
        if not name and self.lane == "correctness":
            name = self.cfg.get("correctness_executor")  # 旧扁平键回落（内存紧可降 subprocess）
        name = name or self.cfg.get("executor", "subprocess")
        return self.executors.get(name) or self.executors["subprocess"]

    def run(self):
        while True:
            job = self.store.next_job(self.lane)
            try:
                self.run_job(job)
            except Exception as exc:  # noqa: BLE001 — worker 必须吞掉一切异常
                job.error = f"worker exception: {exc!r}"
                if job.state not in TERMINAL_STATES:
                    job.state = "failed"
                self.store.finish(job)

    def run_job(self, job):
        store, cfg = self.store, self.cfg
        lane = job.request.get("lane", "timing")
        job.started_at = time.time()
        job.state = "running"
        # 遥测：排队时长（submitted→started）
        store.observe("queue_wait", lane, job.started_at - job.submitted_at)
        jid = job.job_id
        job_dir = store.job_dir(jid)
        os.makedirs(job_dir, exist_ok=True)
        deployed = False
        # 仅 timing 车道持 /tmp/k3_bench.lock 与手工 SSH 基准互斥（ADR-0002，语义不变）；
        # acc/func 车道整段不碰锁——并行筛选是车道存在的全部意义（handoff §3-L2）
        lock_fd = os.open(cfg["lock_file"], os.O_CREAT | os.O_RDWR, 0o644) \
            if lane == "timing" else None
        try:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if job.request.get("files"):
                with open(os.path.join(job_dir, "request.json"), encoding="utf-8") as f:
                    payload = json.load(f)
                try:
                    deploy_files(cfg, job_dir, payload["files"])
                    deployed = True
                except Exception:
                    try:
                        restore_files(job_dir)  # 部署中途失败，按已写 manifest 回滚
                    except Exception as exc:  # noqa: BLE001
                        job.error = f"rollback failed: {exc!r}"
                    raise
            state, result, kope_mem, error = self._executor_for(job.request).run(job)
            job.state, job.result, job.kope_mem, job.error = state, result, kope_mem, error
            if lane in ("correctness", "func") and isinstance(job.result, dict):
                # warmup=0/rep=1 下 speedup 无统计意义；lane 标注供下游甄别
                # （acc 的 correct 仍有效；func 只看跑通，correct 仅参考）
                job.result["lane"] = lane
            if lane == "func" and job.state == "done":
                # 功能冒烟口径：有 JSON 结果行即 pass；不产出 speedup 类计时结论
                job.kope_mem = {"pass_rate": 1.0}
        finally:
            if deployed and job.request.get("restore", True):
                try:
                    restore_files(job_dir)  # 任何路径（含超时/异常）都恢复（ADR-0001）
                except Exception as exc:  # noqa: BLE001
                    job.error = (job.error or "") + f" | restore failed: {exc!r}"
                    # 遥测：restore 失败是 P0（实验环境 §4.6 要求 HALT），必须可见
                    store.count_restore_fail()
            if lock_fd is not None:
                os.close(lock_fd)
            store.finish(job)


# ---------------------------------------------------------------- /metrics 渲染

def _render_metrics(m):
    """把 metrics_snapshot() 渲染为 Prometheus 文本格式（0.0.4）。

    指标名与 textfile 探针版（work/k1-monitor/eval-probe/evald-probe.sh）逐字
    一致，面板按此契约实现；evald_last_job_info 只输出最新一条序列。
    """
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    lines = [
        "# HELP evald_up evald process reachable (1 whenever this endpoint responds).",
        "# TYPE evald_up gauge",
        "evald_up 1",
        "# HELP evald_queued jobs waiting per lane (authoritative in-memory queue).",
        "# TYPE evald_queued gauge",
    ]
    for lane in ("timing", "correctness", "func"):
        lines.append('evald_queued{lane="%s"} %d' % (lane, m["lanes"][lane]["queued"]))
    lines += [
        "# HELP evald_running jobs currently executing per lane.",
        "# TYPE evald_running gauge",
    ]
    for lane in ("timing", "correctness", "func"):
        lines.append('evald_running{lane="%s"} %d' % (lane, m["lanes"][lane]["running"]))
    lines += [
        "# HELP evald_submissions_total jobs submitted since spool creation"
        " (persists across restarts via spool scan).",
        "# TYPE evald_submissions_total counter",
        "evald_submissions_total %d" % m["submissions"],
        "# HELP evald_jobs_total jobs reached terminal state, per state"
        " (persists across restarts via spool scan).",
        "# TYPE evald_jobs_total counter",
    ]
    for state in ("done", "failed", "timeout"):
        lines.append('evald_jobs_total{state="%s"} %d' % (state, m["jobs"].get(state, 0)))
    lines += [
        "# HELP evald_last_submission_timestamp_seconds"
        " epoch time of the most recent submission (0 if none).",
        "# TYPE evald_last_submission_timestamp_seconds gauge",
        "evald_last_submission_timestamp_seconds %.3f" % m["last_sub_ts"],
        "# HELP evald_last_completion_timestamp_seconds"
        " epoch time of the most recent completion (0 if none).",
        "# TYPE evald_last_completion_timestamp_seconds gauge",
        "evald_last_completion_timestamp_seconds %.3f" % m["last_comp_ts"],
        "# HELP evald_last_job_info most recently completed job"
        " (op/state labels, value always 1).",
        "# TYPE evald_last_job_info gauge",
    ]
    if m["last_job"]:
        op, state = m["last_job"]
        lines.append('evald_last_job_info{op="%s",state="%s"} 1' % (esc(op), esc(state)))

    # ---- 最近 3 个已完成作业（idx0=最新）。与 last_job_info 同源：重启由 spool 重建、
    #      finish() 维护；op/state 进 label（面板 K3 卡按 idx/op/state 取值，board 由
    #      Prometheus relabel 注入）。瞬时 gauge，最多 3 条序列/指标，无 bucket 放大。
    #      finished_timestamp 为 epoch 秒（与 last_completion_timestamp 同语义）。
    recent = m.get("recent", [])
    lines += [
        "# HELP evald_recent_job_duration_seconds execution duration (s) of the 3"
        " most recently completed jobs, idx=0 newest (rebuilt from spool on"
        " restart, then maintained in-memory).",
        "# TYPE evald_recent_job_duration_seconds gauge",
    ]
    for i, (op, state, dur, wait, fin) in enumerate(recent):
        if dur is not None:
            lines.append('evald_recent_job_duration_seconds{idx="%d",op="%s",state="%s"} %.3f'
                         % (i, esc(op), esc(state), dur))
    lines += [
        "# HELP evald_recent_job_queue_wait_seconds queue wait (s) of the 3 most"
        " recently completed jobs, idx=0 newest.",
        "# TYPE evald_recent_job_queue_wait_seconds gauge",
    ]
    for i, (op, state, dur, wait, fin) in enumerate(recent):
        if wait is not None:
            lines.append('evald_recent_job_queue_wait_seconds{idx="%d",op="%s",state="%s"} %.3f'
                         % (i, esc(op), esc(state), wait))
    lines += [
        "# HELP evald_recent_job_finished_timestamp_seconds epoch time each of the"
        " 3 most recently completed jobs finished, idx=0 newest.",
        "# TYPE evald_recent_job_finished_timestamp_seconds gauge",
    ]
    for i, (op, state, dur, wait, fin) in enumerate(recent):
        if fin:
            lines.append('evald_recent_job_finished_timestamp_seconds{idx="%d",op="%s",state="%s"} %.3f'
                         % (i, esc(op), esc(state), fin))

    # ---- 进程内遥测（重启清零）。与上面 spool 持久计数严格区分，必须在 HELP 写明，
    #      否则会被误读为 counter reset。
    for kind in ("queue_wait", "job_duration", "psr"):
        metric = HIST_METRIC[kind]
        lines += [
            "# HELP %s %s histogram per lane. PROCESS-LOCAL: resets to zero on"
            " evald restart; unlike evald_submissions_total it is NOT rebuilt"
            " from the spool." % (metric, kind.replace("_", " ")),
            "# TYPE %s histogram" % metric,
        ]
        for lane in ("timing", "correctness", "func"):
            t = m.get("hist", {}).get(kind, {}).get(lane)
            count, total, counts = t if t else (0, 0.0, [0] * len(HIST_BUCKETS[kind]))
            for i, b in enumerate(HIST_BUCKETS[kind]):
                lines.append('%s_bucket{lane="%s",le="%s"} %d'
                             % (metric, lane, _fmt_le(b), counts[i]))
            lines.append('%s_bucket{lane="%s",le=\"+Inf\"} %d' % (metric, lane, count))
            lines.append('%s_sum{lane="%s"} %.3f' % (metric, lane, total))
            lines.append('%s_count{lane="%s"} %d' % (metric, lane, count))
    lines += [
        "# HELP evald_jobs_completed_total jobs reaching terminal state since"
        " evald start, per lane and state (PROCESS-LOCAL: resets on restart).",
        "# TYPE evald_jobs_completed_total counter",
    ]
    for lane in ("timing", "correctness", "func"):
        for state in ("done", "failed", "timeout", "canceled"):
            lines.append('evald_jobs_completed_total{lane="%s",state="%s"} %d'
                         % (lane, state,
                            m.get("completed", {}).get(lane, {}).get(state, 0)))
    lines += [
        "# HELP evald_oldest_queued_age_seconds age of the oldest queued job per"
        " lane (0 when that lane queue is empty).",
        "# TYPE evald_oldest_queued_age_seconds gauge",
    ]
    for lane in ("timing", "correctness", "func"):
        lines.append('evald_oldest_queued_age_seconds{lane="%s"} %.3f'
                     % (lane, m.get("oldest_age", {}).get(lane) or 0.0))
    lines += [
        "# HELP evald_executor_fallback_total warmd failures that fell back to"
        " subprocess, by exception type (previously an invisible failure).",
        "# TYPE evald_executor_fallback_total counter",
    ]
    for reason in sorted(m.get("fallback", {})):
        lines.append('evald_executor_fallback_total{reason="%s"} %d'
                     % (esc(reason), m["fallback"][reason]))
    lines += [
        "# HELP evald_restore_failures_total restore failures after execution"
        " (P0: runbook requires HALT).",
        "# TYPE evald_restore_failures_total counter",
        "evald_restore_failures_total %d" % m.get("restore_fail", 0),
        "# HELP evald_spool_jobs_total job directories under the spool (never"
        " pruned); runtime counter, not a directory scan.",
        "# TYPE evald_spool_jobs_total gauge",
        "evald_spool_jobs_total %d" % m["submissions"],
        "# HELP evald_longpoll_active long-poll /jobs/{id}?wait=N requests"
        " currently suspended.",
        "# TYPE evald_longpoll_active gauge",
        "evald_longpoll_active %d" % m.get("longpoll", 0),
        "# HELP evald_http_requests_total HTTP requests served since evald start,"
        " by normalized path and status (PROCESS-LOCAL: resets on restart).",
        "# TYPE evald_http_requests_total counter",
    ]
    for key in sorted(m.get("http", {}), key=lambda kv: (kv[0], kv[1])):
        path, code = key
        lines.append('evald_http_requests_total{path="%s",code="%d"} %d'
                     % (esc(path), code, m["http"][key]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "evald/1.0"
    store = None
    cfg = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

    # -- helpers
    def _count_http(self, code):
        store = type(self).store
        if store is not None:
            store.count_http(normalize_path(self.path), code)

    def _reply(self, code, obj):
        self._count_http(code)
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get("X-K3-Token") == self.cfg["token"]

    def _reply_metrics(self):
        # 免 token（与 /healthz 同级）：监控抓取端无法发自定义头；本端点只暴露
        # 聚合计数、无作业内容，LAN 内可接受
        self._count_http(200)
        body = _render_metrics(self.store.metrics_snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routing
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            self._reply(200, self._health())
            return
        if u.path == "/metrics":
            self._reply_metrics()
            return
        if not self._authorized():
            self._reply(401, {"error": "bad or missing X-K3-Token"})
            return
        if u.path == "/":
            self._reply(200, {"endpoints": ["/healthz", "/metrics", "/jobs",
                                            "/jobs/{id}?wait=N"]})
        elif u.path == "/jobs":
            self._list_jobs(parse_qs(u.query))
        else:
            parts = [p for p in u.path.split("/") if p]
            if len(parts) == 2 and parts[0] == "jobs":
                self._get_job(parts[1], parse_qs(u.query))
            else:
                self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._reply(401, {"error": "bad or missing X-K3-Token"})
            return
        if urlparse(self.path).path != "/jobs":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, {"error": "bad Content-Length"})
            return
        if length <= 0 or length > self.cfg["body_max_bytes"]:
            self._reply(413, {"error": f"body must be 1..{self.cfg['body_max_bytes']} bytes"})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._reply(400, {"error": f"invalid JSON: {exc}"})
            return
        sanitized, files, err = validate_request(self.cfg, body)
        if err:
            self._reply(400, {"error": err})
            return
        try:
            job = self.store.submit(sanitized, body)
        except QueueFullError:
            self._reply(429, {"error": f"queue full (>{self.cfg['queue_max']})"})
            return
        self._reply(202, {"job_id": job.job_id, "state": "queued",
                          "position": self.store.position(job.job_id)})

    def do_DELETE(self):
        if not self._authorized():
            self._reply(401, {"error": "bad or missing X-K3-Token"})
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 2 and parts[0] == "jobs":
            if self.store.cancel(parts[1]):
                self._reply(200, {"job_id": parts[1], "state": "canceled"})
            else:
                self._reply(409, {"error": "not cancellable (unknown or not queued)"})
        else:
            self._reply(404, {"error": "not found"})

    # -- endpoints
    def _health(self):
        try:
            loadavg = open("/proc/loadavg").read().split()[:3]
        except OSError:
            loadavg = None
        lanes = self.store.lane_stats()
        return {"ok": True,
                "queued": sum(v["queued"] for v in lanes.values()),
                "running": sum(v["running"] for v in lanes.values()),
                "lanes": lanes, "loadavg": loadavg}

    def _list_jobs(self, q):
        try:
            limit = max(1, min(500, int(q.get("limit", ["50"])[0])))
        except ValueError:
            limit = 50
        since = q.get("since", [None])[0]
        jobs = self.store.history(limit, since)
        if jobs is None:
            self._reply(404, {"error": f"unknown since id: {since}"})
            return
        self._reply(200, {"jobs": [j.to_json(full=False) for j in jobs]})

    def _get_job(self, job_id, q):
        job = self.store.get(job_id)
        if job is None:
            self._reply(404, {"error": "unknown job_id"})
            return
        wait = 0
        if q.get("wait"):
            try:
                wait = max(0, min(self.cfg["longpoll_max_s"], int(q["wait"][0])))
            except ValueError:
                pass
        if wait > 0 and job.state not in TERMINAL_STATES:
            # 遥测：长轮询挂起数（此前零信号，连接堆积不可见）
            self.store.bump_longpoll(1)
            try:
                self.store.event_for(job_id).wait(wait)
            finally:
                self.store.bump_longpoll(-1)
            job = self.store.get(job_id) or job
        out = job.to_json(full=True)
        if job.state == "queued":
            out["queue_position"] = self.store.position(job_id)
        self._reply(200, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--gen-token", action="store_true")
    args = ap.parse_args()
    if args.gen_token:
        import secrets
        print(secrets.token_hex(16))
        return
    cfg = load_config(args.config)
    if args.selftest:
        sys.exit(selftest(cfg))

    store = Store(cfg)
    ev = cfg["evaluators"]
    booted = []
    if ev["perf"]["enabled"]:
        # 计时 worker：锁/串行/warmfork 语义一字不动。max_concurrency>1 时多 worker
        # 仍被 /tmp/k3_bench.lock 串行化（计时互斥不变量），并发只影响 pickup 顺序；
        # 配了 perf.cpu_ids 时按 index/total 分核（与 acc/func 同机制）
        n_perf = ev["perf"]["workers"]
        for i in range(n_perf):
            Worker(cfg, store, lane="timing", index=i, total=n_perf).start()
        booted.append(f"perf=timing×{n_perf}")
    if ev["acc"]["enabled"]:
        n_correctness = ev["acc"]["workers"]
        for i in range(n_correctness):
            Worker(cfg, store, lane="correctness", index=i,
                   total=n_correctness).start()
        booted.append(f"acc=correctness×{n_correctness}")
    if ev["func"]["enabled"]:
        n_func = ev["func"]["workers"]
        for i in range(n_func):
            Worker(cfg, store, lane="func", index=i, total=n_func).start()
        booted.append(f"func×{n_func}")
    Handler.store = store
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    httpd.daemon_threads = True

    def _shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    lq = cfg.get("lane_queue_max", {})
    print(f"[evald] listening on {cfg['host']}:{cfg['port']}, data_dir={cfg['data_dir']}, "
          f"evaluators: {', '.join(booted)} "
          f"(cpus perf={ev['perf']['cpus']}, acc={ev['acc']['cpus']}, func={ev['func']['cpus']}; "
          f"lane_queue_max={lq})",
          flush=True)
    httpd.serve_forever()


# ---------------------------------------------------------------- 自检

def selftest(cfg):
    """不监听端口、不碰真实 roots：临时目录里验证白名单/部署-恢复/队列/超时。"""
    failures = []

    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("[1] uuid7")
    u = uuid7()
    check("uuid7 格式", bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", u)), u)
    time.sleep(0.002)
    check("uuid7 时间有序", uuid7() > u)

    print("[2] 白名单")
    ok_paths = ["flag_gems/ops/add.py", "flag_gems/runtime/backend/_spacemit/ops/argmax.py",
                "flag_gems/runtime/backend/_spacemit/tune_configs.yaml",
                "scripts/bench_op.py", "scripts/bench_gemm.py"]
    bad_paths = ["flag_gems/__init__.py", "flag_gems/ops/../core.py", "scripts/run_env.sh",
                 "/etc/passwd", "flag_gems/ops/../../x.py", "scripts/bench_op.py/x",
                 "flag_gems/triton/x.py", "", "flag_gems/ops"]
    tmp = tempfile.mkdtemp(prefix="evald_selftest_")
    cfg = dict(cfg)
    cfg["flag_gems_root"] = os.path.join(tmp, "fg")
    cfg["scripts_root"] = os.path.join(tmp, "scripts")
    os.makedirs(os.path.join(cfg["flag_gems_root"], "ops"), exist_ok=True)
    os.makedirs(os.path.join(cfg["scripts_root"]), exist_ok=True)
    check("白名单接受", all(resolve_path(cfg, p) for p in ok_paths))
    check("白名单拒绝", all(resolve_path(cfg, p) is None for p in bad_paths))

    print("[3] 部署-恢复（含硬链接断链）")
    ops_dir = os.path.join(cfg["flag_gems_root"], "ops")
    orig = os.path.join(ops_dir, "add.py")
    with open(orig, "w") as f:
        f.write("ORIG")
    link = os.path.join(ops_dir, "add_link.py")
    os.link(orig, link)  # 模拟 site-packages nlink=2
    jd = os.path.join(tmp, "job1")
    os.makedirs(jd)
    manifest = deploy_files(cfg, jd, [
        {"path": "flag_gems/ops/add.py", "content": "CANDIDATE"},
        {"path": "flag_gems/ops/new_dir/new.py", "content": "NEW"},
    ])
    check("候选已写入", open(orig).read() == "CANDIDATE")
    check("硬链接已断（原文件未被改写）", open(link).read() == "ORIG")
    check("manifest 记录", manifest[0]["existed"] is True and manifest[1]["existed"] is False)
    restore_files(jd)
    check("恢复原内容", open(orig).read() == "ORIG")
    check("新文件已回收", not os.path.exists(os.path.join(ops_dir, "new_dir", "new.py")))
    check("空目录已清理", not os.path.exists(os.path.join(ops_dir, "new_dir")))

    print("[4] 队列 + 假 bench")
    cfg["data_dir"] = os.path.join(tmp, "data")
    cfg["executor"] = "subprocess"  # 本节测子进程执行器路径（默认已是 warmfork）
    os.makedirs(cfg["data_dir"], exist_ok=True)
    store = Store(cfg)
    echo_row = json.dumps({"op": "fake", "torch_ms": 2.0, "gems_ms": 1.0,
                           "speedup": 2.0, "correct": True, "max_abs_err": 0.0})
    cfg["bench_command_override"] = f"echo '{echo_row}'"  # selftest 专用钩子
    worker = Worker(cfg, store)
    req1 = {"op": "fake", "shape": "1024", "dtype": "float32", "warmup": 1,
            "rep": 1, "bench": "op", "restore": True, "timeout_min": 1, "files": []}
    job = store.submit(req1, req1)
    worker.run_job(store.get(job.job_id))
    j = store.get(job.job_id)
    check("假 bench done", j.state == "done", j.state)
    check("结果解析+speedup", j.result and abs(j.result["speedup"] - 2.0) < 1e-9)
    check("kope_mem 兼容", j.kope_mem == {"speedup": 2.0, "pass_rate": 1.0})
    check("jsonl 已落盘", "jobs.jsonl" in os.listdir(cfg["data_dir"]))

    print("[5] 超时路径")
    cfg2 = dict(cfg)
    cfg2["data_dir"] = os.path.join(tmp, "data2")
    os.makedirs(cfg2["data_dir"], exist_ok=True)
    cfg2["bench_command_override"] = "sleep 30"
    store2 = Store(cfg2)
    worker2 = Worker(cfg2, store2)
    req2 = {"op": "fake", "shape": "1024", "dtype": "float32", "warmup": 1,
            "rep": 1, "bench": "op", "restore": True, "timeout_min": 0.1, "files": []}
    job2 = store2.submit(req2, req2)
    t0 = time.time()
    worker2.run_job(store2.get(job2.job_id))
    j2 = store2.get(job2.job_id)
    check("超时标记", j2.state == "timeout", j2.state)
    check("超时及时返回", time.time() - t0 < 15)

    if hasattr(os, "fork"):
        print("[6] warmd 协议（WARMD_FAKE，不依赖 torch）")
        warmd_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warmd.py")
        check("warmd.py 存在", os.path.exists(warmd_src))
        stub_bin = os.path.join(tmp, "stubbin")
        os.makedirs(stub_bin, exist_ok=True)
        try:
            os.symlink(sys.executable, os.path.join(stub_bin, "python"))
        except (OSError, NotImplementedError):
            shutil.copy(sys.executable, os.path.join(stub_bin, "python"))
        stub_env = os.path.join(tmp, "run_env_stub.sh")
        with open(stub_env, "w") as f:
            f.write("#!/bin/bash\nexport PATH=\"%s:$PATH\"\n" % stub_bin)
        cfg3 = dict(cfg)
        cfg3["data_dir"] = os.path.join(tmp, "data3")
        os.makedirs(cfg3["data_dir"], exist_ok=True)
        cfg3["run_env"] = stub_env
        cfg3["warmd_path"] = warmd_src
        cfg3["warmd_env"] = {"WARMD_FAKE": "1"}
        cfg3["executor"] = "warmfork"
        store3 = Store(cfg3)
        worker3 = Worker(cfg3, store3)
        req3 = {"op": "fake", "shape": "1024", "dtype": "float32", "warmup": 1,
                "rep": 1, "bench": "op", "restore": True, "timeout_min": 1,
                "executor": "warmfork", "files": []}
        job3 = store3.submit(req3, req3)
        worker3.run_job(store3.get(job3.job_id))
        j3 = store3.get(job3.job_id)
        check("warmfork 假作业 done", j3.state == "done", j3.state)
        check("warmfork speedup=2.0", (j3.result or {}).get("speedup") == 2.0)
        check("warmfork kope_mem", j3.kope_mem == {"speedup": 2.0, "pass_rate": 1.0})

        print("[7] warmd 子进程超时")
        cfg4 = dict(cfg3)
        cfg4["data_dir"] = os.path.join(tmp, "data4")
        os.makedirs(cfg4["data_dir"], exist_ok=True)
        cfg4["warmd_env"] = {"WARMD_FAKE": "1", "WARMD_FAKE_SLEEP": "30"}
        store4 = Store(cfg4)
        worker4 = Worker(cfg4, store4)
        req4 = dict(req3, timeout_min=0.1)
        job4 = store4.submit(req4, req4)
        t0 = time.time()
        worker4.run_job(store4.get(job4.job_id))
        j4 = store4.get(job4.job_id)
        check("warmd 超时标记", j4.state == "timeout", j4.state)
        check("warmd 超时及时返回", time.time() - t0 < 15)
    else:
        print("[6]/[7] warmd 协议测试需要 POSIX fork，本平台跳过")

    print("[8] op 名校验（RE_OP）")
    ok_ops = ["add", "_reshape_alias",
              "functional_sym_constrain_range_for_size",
              "special_scaled_modified_bessel_k1",
              "special_shifted_chebyshev_polynomial_v", "a" * 48]
    bad_ops = ["Add", "1add", "add.py", "a-b", "a/b", "a b", "", "a" * 49]
    check("op 名放宽后接受（前导下划线/长名）", all(RE_OP.match(p) for p in ok_ops))
    check("op 名拒绝非法字符/超长", all(not RE_OP.match(p) for p in bad_ops))

    print("[9] lane 字段校验与 correctness 注入")
    good = {"op": "add", "shape": "64,512,512", "dtype": "float32",
            "warmup": 10, "rep": 50, "bench": "op", "restore": True}
    s0, _, e0 = validate_request(cfg, dict(good))
    check("无 lane 请求不带 lane 键（向后逐字节兼容）", e0 is None and "lane" not in s0)
    check("无 lane 请求 warmup/rep 原样", s0["warmup"] == 10 and s0["rep"] == 50)
    # correctness 车道 feature 断言需要 acc 评估器启用；生产 config 可能停用 acc
    # （K3 计时板 correctness_workers=0）→ 用强制启用 acc 的 config 变体产出 s1，
    # 与生产开关解耦（否则 s1=None 会让本节及复用 s1 的 [10]-[15] 全崩）。
    cfg_acc = dict(cfg)
    cfg_acc["evaluators"] = {k: dict(v) for k, v in cfg["evaluators"].items()}
    cfg_acc["evaluators"]["acc"] = dict(cfg["evaluators"].get("acc", {}),
                                        enabled=True, workers=2, cpus="4-6",
                                        omp_threads=1, warmup_times=0, repeat_times=1)
    s1, _, e1 = validate_request(cfg_acc, dict(good, lane="correctness"))
    check("correctness 车道接受（acc 启用）", e1 is None and s1["lane"] == "correctness")
    check("correctness 覆盖 warmup=0/rep=1", s1["warmup"] == 0 and s1["rep"] == 1)
    check("correctness 注记原值", s1["requested_warmup"] == 10 and s1["requested_rep"] == 50)
    if not cfg["evaluators"]["acc"]["enabled"]:
        _, _, e_off = validate_request(cfg, dict(good, lane="correctness"))
        check("acc 停用时 correctness 车道被拒（生产开关生效，防作业无人消费）",
              e_off is not None and "未启用" in e_off, e_off)
    s2, _, e2 = validate_request(cfg, dict(good, lane="timing"))
    check("显式 timing 车道接受", e2 is None and s2["lane"] == "timing")
    _, _, e3 = validate_request(cfg, dict(good, lane="banana"))
    check("非法 lane 拒绝（HTTP 400 同源）", e3 is not None, e3)
    cmd_t = SubprocessExecutor(cfg).build_command(s0)
    check("timing 命令逐字节不变", cmd_t ==
          (f"source {cfg['run_env']} && python {cfg['scripts_root']}/bench_op.py "
           f"--op add --shape 64,512,512 --dtype float32 --warmup 10 --rep 50"), cmd_t)
    cmd_c = SubprocessExecutor(cfg_acc, lane="correctness").build_command(s1)
    check("correctness 命令注入 K3_LANE/OMP/--warmup 0 --rep 1",
          "K3_LANE=correctness" in cmd_c and "OMP_NUM_THREADS=1" in cmd_c
          and "--warmup 0 --rep 1" in cmd_c, cmd_c)
    if shutil.which("taskset"):
        check("correctness 进程 taskset 核亲和（Popen argv）",
              SubprocessExecutor(cfg_acc, lane="correctness")._taskset_argv()
              == ["taskset", "-c", str(cfg_acc["evaluators"]["acc"]["cpus"])])
        check("timing 车道不加 taskset（语义不变）",
              SubprocessExecutor(cfg)._taskset_argv() == [])
    else:
        print("  （本平台无 taskset，核亲和断言由板上 selftest 覆盖）")

    print("[10] 车道队列路由与共享 queue_max")
    cfg_r = dict(cfg)
    cfg_r["data_dir"] = os.path.join(tmp, "dataR")
    os.makedirs(cfg_r["data_dir"], exist_ok=True)
    store_r = Store(cfg_r)
    jt = store_r.submit(dict(s0), dict(s0))
    jc = store_r.submit(dict(s1), dict(s1))
    check("timing 作业入 timing 队列", jt.job_id in store_r.queues["timing"])
    check("correctness 作业入 correctness 队列", jc.job_id in store_r.queues["correctness"])
    stats = store_r.lane_stats()
    check("lane_stats 每车道计数", stats == {"timing": {"queued": 1, "running": 0},
                                            "correctness": {"queued": 1, "running": 0},
                                            "func": {"queued": 0, "running": 0}}, stats)
    check("position 按车道独立", store_r.position(jt.job_id) == 1
          and store_r.position(jc.job_id) == 1)
    check("next_job 车道分派", store_r.next_job("correctness").job_id == jc.job_id
          and store_r.next_job("timing").job_id == jt.job_id)
    jt2 = store_r.submit(dict(s0), dict(s0))
    check("cancel 从所属车道队列移除", store_r.cancel(jt2.job_id)
          and len(store_r.queues["timing"]) == 0)
    cfg_q = dict(cfg_r)
    cfg_q["data_dir"] = os.path.join(tmp, "dataQ")
    os.makedirs(cfg_q["data_dir"], exist_ok=True)
    cfg_q["queue_max"] = 2
    store_q = Store(cfg_q)
    store_q.submit(dict(s0), dict(s0))
    store_q.submit(dict(s1), dict(s1))
    try:
        store_q.submit(dict(s1), dict(s1))
        full = False
    except QueueFullError:
        full = True
    check("queue_max 跨车道共享", full)

    print("[11] 锁语义：correctness 不获取 bench 锁（flock 探针）")
    cfg_l = dict(cfg)
    cfg_l["data_dir"] = os.path.join(tmp, "dataL")
    os.makedirs(cfg_l["data_dir"], exist_ok=True)
    cfg_l["executor"] = "subprocess"
    cfg_l["lock_file"] = os.path.join(tmp, "probe_bench.lock")
    cfg_l["bench_command_override"] = f"echo '{echo_row}'"
    store_l = Store(cfg_l)
    probe = os.open(cfg_l["lock_file"], os.O_CREAT | os.O_RDWR, 0o644)
    real_lock = True  # 本地 fcntl shim（无真实互斥）时降级为仅测 correctness 不碰锁
    try:
        fcntl.flock(probe, fcntl.LOCK_EX)  # 人为持锁：模拟手工 SSH 基准进行中
        probe2 = os.open(cfg_l["lock_file"], os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(probe2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe2, fcntl.LOCK_UN)
            real_lock = False  # 第二 fd 也能拿锁 ⇒ 非真实互斥语义（shim 降级）
        except OSError:
            pass
        os.close(probe2)
    except (OSError, AttributeError):
        real_lock = False
    jc2 = store_l.submit(dict(s1), dict(s1))
    Worker(cfg_l, store_l, lane="correctness").run_job(store_l.get(jc2.job_id))
    j = store_l.get(jc2.job_id)
    check("持锁状态下 correctness 作业照常完成", j.state == "done", (j.state, j.error))
    check("correctness 结果带 lane 标注", (j.result or {}).get("lane") == "correctness")
    if real_lock:
        t_worker = Worker(cfg_l, store_l)  # timing worker：应被探针锁挡住
        jt3 = store_l.submit(dict(s0), dict(s0))
        th = threading.Thread(target=t_worker.run_job,
                              args=(store_l.get(jt3.job_id),), daemon=True)
        th.start()
        time.sleep(1.5)
        blocked = store_l.get(jt3.job_id).finished_at is None
        fcntl.flock(probe, fcntl.LOCK_UN)  # 释放锁
        th.join(timeout=30)
        check("timing 作业持锁期间排队、释放后完成",
              blocked and store_l.get(jt3.job_id).state == "done")
    else:
        print("  （本平台 flock 无真实互斥语义，timing 排队断言由板上 selftest 覆盖）")
    os.close(probe)

    print("[12] correctness 3 worker 并行互不阻塞（真实 Worker 线程）")
    cfg_p = dict(cfg_l)
    cfg_p["data_dir"] = os.path.join(tmp, "dataP")
    os.makedirs(cfg_p["data_dir"], exist_ok=True)
    cfg_p["bench_command_override"] = (
        f"sleep 2; echo '{echo_row}'; echo \"K3_LANE=$K3_LANE\"; "
        "grep Cpus_allowed_list /proc/self/status || true")
    store_p = Store(cfg_p)
    pool = [Worker(cfg_p, store_p)]  # timing worker 空转占位（锁路径无人为持锁）
    pool += [Worker(cfg_p, store_p, lane="correctness", index=i) for i in range(3)]
    for w in pool:
        w.start()
    c_ids = [store_p.submit(dict(s1), dict(s1)) for _ in range(3)]
    tmix = store_p.submit(dict(s0), dict(s0))  # 混提 1 个 timing 作业
    t0 = time.time()
    all_ids = c_ids + [tmix]
    while time.time() - t0 < 60 and not all(
            store_p.get(x.job_id).state in TERMINAL_STATES for x in all_ids):
        time.sleep(0.1)
    wall = time.time() - t0
    check("3 correctness + 1 timing 混提全部 done",
          all(store_p.get(x.job_id).state == "done" for x in all_ids),
          [store_p.get(x.job_id).state for x in all_ids])
    check("3 correctness 并行墙钟远小于串行（互不阻塞）", wall < 5.5, f"wall={wall:.1f}s")
    check("K3_LANE 注入 correctness bench 进程",
          all("K3_LANE=correctness" in store_p.get(x.job_id).stdout for x in c_ids))
    check("timing bench 进程不被注入 K3_LANE",
          "K3_LANE=correctness" not in store_p.get(tmix.job_id).stdout)
    if shutil.which("taskset") and os.path.exists("/proc/self/status"):
        cpus = str(cfg_p["evaluators"]["acc"]["cpus"])  # acc 车道生效核（非旧扁平键）
        check("correctness bench 进程 affinity=车道核（end-to-end）",
              all(f"Cpus_allowed_list:\t{cpus}" in store_p.get(x.job_id).stdout
                  for x in c_ids))
        # timing 车道不钉核：bench 进程 affinity 应等于本进程全核（正向判据）。
        # 旧判据「不含 acc 车道核串」在 acc=全核（如 K1 8 核板配 cpu_ids 0-7）时恒假。
        full = _format_cpu_ids(os.sched_getaffinity(0))
        check("timing bench 进程 affinity=全核（不钉车道核）",
              f"Cpus_allowed_list:\t{full}" in store_p.get(tmix.job_id).stdout,
              store_p.get(tmix.job_id).stdout[-200:])

    print("[13] /metrics 计数器（spool 持久语义）")
    cfg_m = dict(cfg)
    cfg_m["data_dir"] = os.path.join(tmp, "dataM")
    os.makedirs(cfg_m["data_dir"], exist_ok=True)
    store_m = Store(cfg_m)
    snap0 = store_m.metrics_snapshot()
    check("空 spool 初值", snap0["submissions"] == 0
          and snap0["jobs"] == {"done": 0, "failed": 0, "timeout": 0}, snap0)
    jm1 = store_m.submit(dict(s0), dict(s0))
    jm2 = store_m.submit(dict(s1), dict(s1))
    store_m.get(jm1.job_id).state = "done"
    store_m.finish(store_m.get(jm1.job_id))
    time.sleep(0.01)  # 秒级 mtime 粒度的文件系统下保证先后可辨
    store_m.get(jm2.job_id).state = "failed"
    store_m.finish(store_m.get(jm2.job_id))
    snap = store_m.metrics_snapshot()
    check("submissions 累计 +2", snap["submissions"] == 2, snap)
    check("jobs_total done/failed 各 +1",
          snap["jobs"] == {"done": 1, "failed": 1, "timeout": 0}, snap["jobs"])
    check("last_job_info 指向最新完成", snap["last_job"] == ("add", "failed"),
          snap["last_job"])
    check("last_submission ≤ last_completion",
          0 < snap["last_sub_ts"] <= snap["last_comp_ts"])
    text = _render_metrics(snap)
    check("文本契约逐字（计数/info 行）",
          "evald_submissions_total 2" in text
          and 'evald_jobs_total{state="done"} 1' in text
          and 'evald_jobs_total{state="failed"} 1' in text
          and 'evald_last_job_info{op="add",state="failed"} 1' in text
          and text.startswith("# HELP evald_up"), text)
    store_m2 = Store(cfg_m)  # 模拟重启：同一 spool 重新扫描
    snap2 = store_m2.metrics_snapshot()
    check("重启后计数无缝衔接",
          snap2["submissions"] == 2
          and snap2["jobs"] == {"done": 1, "failed": 1, "timeout": 0}
          and snap2["last_job"] == ("add", "failed")
          and abs(snap2["last_comp_ts"] - snap["last_comp_ts"]) < 1)

    print("[14] 评估器 feature flags（perf/acc/func）")

    def ev_rejects(cand):
        try:
            normalize_evaluators(dict(cand))
            return False
        except SystemExit:
            return True

    base = dict(cfg)
    base.pop("evaluators", None)
    ev0 = normalize_evaluators(dict(base))["evaluators"]
    check("缺省推导保持旧版（perf+acc 开、func 关）",
          [ev0[n]["enabled"] for n in ("perf", "acc", "func")] == [True, True, False], ev0)
    b2 = dict(base)
    b2["correctness_workers"] = 0
    check("correctness_workers=0 推导 acc 关",
          normalize_evaluators(dict(b2))["evaluators"]["acc"]["enabled"] is False)
    c1 = dict(base)
    c1["evaluators"] = {"perf": {"enabled": False}, "acc": {"enabled": False},
                        "func": {"enabled": False}}
    check("全停拒绝启动", ev_rejects(c1))
    c2 = dict(base)
    c2["evaluators"] = {"perf": {"enabled": True}, "banana": {"enabled": True}}
    check("未知评估器名拒绝", ev_rejects(c2))
    c3 = dict(base)
    c3["evaluators"] = {"acc": {"enabled": True, "warmup": 5}}
    check("评估器节未知键拒绝", ev_rejects(c3))
    c4 = dict(base)
    c4["evaluators"] = {"perf": {"enabled": False},
                        "acc": {"enabled": True, "warmup_times": 5}}
    check("acc 配非零 warmup_times 拒绝（恒 0）", ev_rejects(c4))
    c5 = dict(base)
    c5["evaluators"] = {"acc": {"enabled": True},
                        "func": {"enabled": True, "repeat_times": 3}}
    check("func 配非 1 repeat_times 拒绝（恒 1）", ev_rejects(c5))
    c6 = dict(base)
    c6["evaluators"] = {"acc": {"enabled": True, "workers": 0}}
    check("acc workers=0 拒绝", ev_rejects(c6))
    c7 = dict(base)
    c7["evaluators"] = {"perf": {"enabled": False}, "acc": {"enabled": True}}
    c7["force_lane"] = "timing"
    check("force_lane 指向停用评估器拒绝", ev_rejects(c7))
    c8 = dict(base)
    c8["evaluators"] = {"perf": {"enabled": True, "warmup_times": 5, "repeat_times": 5},
                        "acc": {"enabled": True, "workers": 2, "cpus": "2-3"},
                        "func": {"enabled": True, "workers": 1}}
    cfg_g = normalize_evaluators(dict(c8))
    check("归一化补全 acc/func 轮数与资源（func 资源随 acc）",
          cfg_g["evaluators"]["acc"]["warmup_times"] == 0
          and cfg_g["evaluators"]["acc"]["repeat_times"] == 1
          and cfg_g["evaluators"]["func"]["cpus"] == "2-3"
          and cfg_g["evaluators"]["func"]["omp_threads"] == 1,
          cfg_g["evaluators"])

    s3, _, eg = validate_request(cfg_g, {"op": "add", "shape": "64", "dtype": "float32"})
    check("perf 请求缺省轮数取 evaluators.perf",
          eg is None and s3["warmup"] == 5 and s3["rep"] == 5,
          (eg, s3.get("warmup"), s3.get("rep")))
    s4, _, eg2 = validate_request(cfg_g, {"op": "add", "shape": "64", "dtype": "float32",
                                          "warmup": 9, "rep": 9})
    check("perf 请求显式轮数覆盖配置", eg2 is None and s4["warmup"] == 9 and s4["rep"] == 9)
    c9 = dict(cfg_g)
    c9["evaluators"] = dict(cfg_g["evaluators"], perf={"enabled": False})
    _, _, eg3 = validate_request(c9, {"op": "add", "shape": "64", "dtype": "float32"})
    check("perf 停用：缺省 lane=timing 拒绝", eg3 is not None, eg3)
    _, _, eg4 = validate_request(c9, {"op": "add", "shape": "64", "dtype": "float32",
                                      "lane": "timing"})
    check("perf 停用：显式 timing 拒绝", eg4 is not None and "perf" in eg4, eg4)
    s5, _, eg5 = validate_request(c9, {"op": "add", "shape": "64", "dtype": "float32",
                                       "lane": "func", "warmup": 7, "rep": 9})
    check("func 车道接受", eg5 is None and s5["lane"] == "func", eg5)
    check("func 强制 warmup=0/rep=1 并注记原值",
          s5["warmup"] == 0 and s5["rep"] == 1
          and s5["requested_warmup"] == 7 and s5["requested_rep"] == 9, s5)
    _, _, eg6 = validate_request(c9, {"op": "add", "shape": "64", "dtype": "float32",
                                      "lane": "banana"})
    check("非法 lane 拒绝（含 func 枚举）", eg6 is not None, eg6)

    cfg_f = normalize_evaluators(dict(c8))
    cfg_f["data_dir"] = os.path.join(tmp, "dataF")
    os.makedirs(cfg_f["data_dir"], exist_ok=True)
    cfg_f["executor"] = "subprocess"
    func_row = json.dumps({"op": "fake", "torch_ms": 2.0, "gems_ms": 1.0,
                           "speedup": 2.0, "correct": None, "max_abs_err": None})
    cfg_f["bench_command_override"] = f"echo '{func_row}'"
    store_f = Store(cfg_f)
    reqf = {"op": "fake", "shape": "64", "dtype": "float32", "warmup": 0, "rep": 1,
            "bench": "op", "restore": True, "timeout_min": 1, "lane": "func", "files": []}
    jf = store_f.submit(reqf, dict(reqf))
    check("func 作业路由到 func 队列", jf.job_id in store_f.queues["func"])
    Worker(cfg_f, store_f, lane="func", index=0).run_job(store_f.get(jf.job_id))
    jfr = store_f.get(jf.job_id)
    check("func 作业 done（correct=None 不影响 pass）", jfr.state == "done",
          (jfr.state, jfr.error))
    check("func 结果带 lane=func 标注", (jfr.result or {}).get("lane") == "func",
          jfr.result)
    check("func kope_mem 只有 pass_rate（无 speedup）",
          jfr.kope_mem == {"pass_rate": 1.0}, jfr.kope_mem)
    cmd_f = SubprocessExecutor(cfg_f, lane="func").build_command(dict(s5))
    check("func 命令注入 K3_LANE=func 与 --warmup 0 --rep 1",
          "K3_LANE=func" in cmd_f and "OMP_NUM_THREADS=1" in cmd_f
          and "--warmup 0 --rep 1" in cmd_f, cmd_f)
    if shutil.which("taskset"):
        check("func 进程 taskset 用 func 车道核",
              SubprocessExecutor(cfg_f, lane="func")._taskset_argv()
              == ["taskset", "-c", "2-3"])
    check("lane_stats 含 func 车道", store_f.lane_stats().get("func") is not None)
    check("metrics 渲染含 lane=func 序列",
          'evald_queued{lane="func"}' in _render_metrics(store_f.metrics_snapshot()))

    print("[15] /metrics 遥测信号（进程内语义，重启清零）")
    store_m.observe("queue_wait", "timing", 2.5)
    store_m.observe("queue_wait", "timing", 42.0)
    store_m.observe("job_duration", "timing", 84.5)
    store_m.observe("psr", "timing", 12.0)
    store_m.count_fallback("RuntimeError")
    store_m.count_restore_fail()
    store_m.count_http("/jobs/{id}", 200)
    snap15 = store_m.metrics_snapshot()
    text15 = _render_metrics(snap15)
    for name in ("evald_queue_wait_seconds_bucket", "evald_job_duration_seconds_bucket",
                 "evald_psr_bucket", "evald_jobs_completed_total",
                 "evald_oldest_queued_age_seconds", "evald_executor_fallback_total",
                 "evald_restore_failures_total", "evald_spool_jobs_total",
                 "evald_longpoll_active", "evald_http_requests_total"):
        check(f"新指标存在 {name}", name in text15, name)
    qw = snap15["hist"]["queue_wait"]["timing"]
    counts15 = qw[2]
    check("queue_wait 累积语义（2.5→≥5 桶，42→≥60 桶）",
          qw[0] == 2 and counts15[1] == 1 and counts15[4] == 2, (qw[0], counts15))
    check("bucket 计数单调不减",
          all(counts15[i] <= counts15[i + 1] for i in range(len(counts15) - 1)), counts15)
    check("histogram _sum ≤ _count × 最大 bucket",
          0 < qw[1] <= qw[0] * HIST_BUCKETS["queue_wait"][-1], qw)
    check("进程内语义写进 # HELP（否则被误读为 counter reset）",
          "resets to zero on" in text15)
    # [13] 的 jm1 是无 lane 请求（→timing/done），jm2 是 correctness（→failed）
    check("jobs_completed 按 lane/state 分列",
          'evald_jobs_completed_total{lane="timing",state="done"} 1' in text15
          and 'evald_jobs_completed_total{lane="correctness",state="failed"} 1' in text15,
          [ln for ln in text15.splitlines() if "jobs_completed_total" in ln])
    check("fallback/restore/http 计数可见",
          'evald_executor_fallback_total{reason="RuntimeError"} 1' in text15
          and "evald_restore_failures_total 1" in text15
          and 'evald_http_requests_total{path="/jobs/{id}",code="200"} 1' in text15)
    # 归一发生在 Handler._count_http（渲染前），这里直接测纯函数
    check("path 归一：作业 id 不进 label",
          normalize_path("/jobs/01a08371-4268-718f-a7b9-1feb0b32b45d") == "/jobs/{id}"
          and normalize_path("/jobs/abc/stdout") == "/jobs/{id}/stdout"
          and normalize_path("/jobs?since=x") == "/jobs"
          and normalize_path("/metrics") == "/metrics",
          [normalize_path(p) for p in
           ("/jobs/01a08371", "/jobs/abc/stdout", "/jobs?since=x", "/metrics")])

    print("[16] 多 shape 批量（cases[] 契约）")
    cfg16 = dict(cfg)
    cfg16["data_dir"] = os.path.join(tmp, "data16")
    os.makedirs(cfg16["data_dir"], exist_ok=True)
    shapes16 = ["64,512,512", "128,256,256"]
    req16 = {"op": "add", "shape": shapes16, "dtype": "float32", "warmup": 1,
             "rep": 1, "bench": "op", "restore": True, "timeout_min": 2, "files": []}
    san16, _, err16 = validate_request(cfg16, dict(req16))
    check("多 shape 通过校验", err16 is None and san16
          and san16["shape"] == shapes16, err16)
    _, _, e_bad = validate_request(cfg16, dict(req16, shape=["64,512,512", "not-a-shape"]))
    check("含非法 shape 拒绝", e_bad is not None and "不合法" in e_bad, e_bad)
    _, _, e_dup = validate_request(cfg16, dict(req16, shape=["1024", "1024"]))
    check("重复 shape 拒绝", e_dup is not None, e_dup)
    _, _, e_big = validate_request(cfg16, dict(req16, shape=["%d" % i for i in range(20)]))
    check("超 MAX_SHAPES 拒绝", e_big is not None, e_big)
    _, _, e_to = validate_request(cfg16, dict(req16, timeout_min=999))
    check("整批 timeout 超限拒绝", e_to is not None, e_to)
    # 真实 bench_op.py 会把 shape 输出成 list（如 [64,512,512]）；这里照此构造，
    # 验证 _map_rows 用提交字符串覆盖（否则 cases[i].shape 会是 list，契约漂移）
    rows16 = [json.dumps({"op": "add", "shape": [int(x) for x in s.split(",")],
                          "gems_ms": 1.0 + i, "torch_ms": 2.0,
                          "speedup": 2.0, "correct": True})
              for i, s in enumerate(shapes16)]
    cfg16["bench_command_override"] = "printf '%s\\n' " + \
        " ".join("'%s'" % r for r in rows16)
    store16 = Store(cfg16)
    j16 = store16.submit(dict(req16), dict(req16))
    Worker(cfg16, store16).run_job(store16.get(j16.job_id))
    r16 = store16.get(j16.job_id)
    check("多 shape 作业 done", r16.state == "done", (r16.state, r16.error))
    check("顶层无 speedup 标量（防老驱动只看第一个 shape）",
          isinstance(r16.result, dict) and "speedup" not in r16.result, r16.result)
    check("result.cases 按提交顺序标注 shape",
          [c.get("shape") for c in r16.result.get("cases", [])] == shapes16, r16.result)
    check("cases_expected == cases_returned，无 partial",
          r16.result["cases_expected"] == 2 and r16.result["cases_returned"] == 2
          and "partial" not in r16.result, r16.result)
    check("kope_mem 不给歧义 speedup（只给 pass_rate）",
          set(r16.kope_mem or {}) == {"pass_rate"}, r16.kope_mem)
    # 部分结果：只回来 1 行 → 必须显式标记 partial，不能静默当成功
    cfg16b = dict(cfg16)
    cfg16b["data_dir"] = os.path.join(tmp, "data16b")
    os.makedirs(cfg16b["data_dir"], exist_ok=True)
    one_row = json.dumps({"op": "add", "gems_ms": 1.0, "torch_ms": 2.0,
                          "speedup": 2.0, "correct": True})
    cfg16b["bench_command_override"] = f"printf '%s\\n' '{one_row}'"
    store16b = Store(cfg16b)
    jb = store16b.submit(dict(req16), dict(req16))
    Worker(cfg16b, store16b).run_job(store16b.get(jb.job_id))
    rb = store16b.get(jb.job_id)
    check("部分结果显式标 partial",
          (rb.result or {}).get("partial") is True
          and rb.result["cases_returned"] == 1, rb.result)
    # 降级路径（warmd 故障走 subprocess）：多 shape 必须逐 shape 出命令
    cmd16 = SubprocessExecutor(cfg16).build_command(dict(req16))
    check("多 shape 命令逐 shape 展开（&& 串联）",
          cmd16.count("--shape") == 2 and " && " in cmd16, cmd16)

    print("[17] 最近 3 个作业指标（recent-job gauges，面板 K3 卡契约）")
    cfg17 = dict(cfg)
    cfg17["data_dir"] = os.path.join(tmp, "data17")
    cfg17["executor"] = "subprocess"      # 不走 warmfork，避免 selftest 拉起 warmd
    os.makedirs(cfg17["data_dir"], exist_ok=True)
    row17 = json.dumps({"op": "add", "torch_ms": 2.0, "gems_ms": 1.0,
                        "speedup": 2.0, "correct": True, "max_abs_err": 0.0})
    cfg17["bench_command_override"] = f"echo '{row17}'"
    store17 = Store(cfg17)
    for op in ("add", "mul", "sub", "div"):     # 4 个作业 → ring buffer 只留最近 3
        r17 = {"op": op, "shape": "1024", "dtype": "float32", "warmup": 1,
               "rep": 1, "bench": "op", "restore": True, "timeout_min": 1, "files": []}
        j17 = store17.submit(r17, r17)
        Worker(cfg17, store17).run_job(store17.get(j17.job_id))
        time.sleep(0.02)                        # 保证 result.json mtime 严格递增
    snap17 = store17.metrics_snapshot()
    text17 = _render_metrics(snap17)
    rec17 = snap17["recent"]
    check("ring buffer 只保留最近 3 条", len(rec17) == 3, rec17)
    check("idx0=最新，完成倒序 div>sub>mul（add 已淘汰）",
          [t[0] for t in rec17] == ["div", "sub", "mul"], [t[0] for t in rec17])
    for name in ("evald_recent_job_duration_seconds",
                 "evald_recent_job_queue_wait_seconds",
                 "evald_recent_job_finished_timestamp_seconds"):
        check(f"recent 指标存在 {name}", name in text17, name)
    check("recent 指标带 idx/op/state 三标签，idx0=div/done",
          'evald_recent_job_duration_seconds{idx="0",op="div",state="done"}' in text17,
          [ln for ln in text17.splitlines() if "recent_job_duration" in ln])
    fin_lines = [ln for ln in text17.splitlines()
                 if ln.startswith("evald_recent_job_finished_timestamp_seconds{")]
    check("finished_timestamp 为 epoch 秒（>1e9；面板 now-ts 算「多久之前」）",
          len(fin_lines) == 3
          and all(float(ln.rsplit(" ", 1)[1]) > 1e9 for ln in fin_lines),
          fin_lines)
    # 重启重建：新 Store 扫 spool 应恢复最近 3 条（与 last_job_info 同源语义）
    rec17b = Store(cfg17).metrics_snapshot()["recent"]
    check("重启后从 spool 重建最近 3 条（顺序一致）",
          [t[0] for t in rec17b] == ["div", "sub", "mul"], [t[0] for t in rec17b])

    print("[18] lane cpus 分核（每 worker 独占一段，不可切分回落整段）")
    ok18 = (_split_cpus("0-7", 0, 2) == "0-3"
            and _split_cpus("0-7", 1, 2) == "4-7"
            and _split_cpus("2-7", 0, 4) == "2-3"      # 6核/4人：前两份多 1
            and _split_cpus("2-7", 3, 4) == "7"
            and _split_cpus("2-5,8", 2, 4) == "5"      # 非连续段也能切
            and _split_cpus("0-2", 1, 4) is None)      # CPU 数不足 → 回落
    check("均分/余数/非连续段/回落", ok18)
    cfg18 = dict(cfg)
    cfg18["evaluators"] = {"perf": {"enabled": True},
                           "acc": {"enabled": True, "workers": 2,
                                   "cpus": "0-7", "omp_threads": 4},
                           "func": {"enabled": False}}
    check("lane_resources 分核生效",
          lane_resources(cfg18, "correctness", 1, 2) == ("4-7", 4))
    check("lane_resources 不传 index/total 维持整段共享",
          lane_resources(cfg18, "correctness") == ("0-7", 4))
    cfg18b = dict(cfg)
    cfg18b["evaluators"] = {"perf": {"enabled": True},
                            "acc": {"enabled": True, "workers": 2,
                                    "cpus": "0-7", "omp_threads": 4,
                                    "split_cpus": False},
                            "func": {"enabled": False}}
    check("split_cpus=false 回落整段共享（软限制对照）",
          lane_resources(cfg18b, "correctness", 1, 2) == ("0-7", 4))
    cfg18c = dict(cfg)
    cfg18c["evaluators"] = {"perf": {"enabled": True},
                            "acc": {"enabled": True, "workers": 4,
                                    "cpus": "2-7", "omp_threads": 1},
                            "func": {"enabled": False}}
    cfg18c["perf"] = {"acc": {"max_concurrency": 2, "cpu_ids": "0-7",
                             "omp_threads": 4, "split_cpus": False}}
    normalize_perf_resources(cfg18c)
    acc18 = cfg18c["evaluators"]["acc"]
    check("perf 覆盖层归并 workers/cpus/omp/split_cpus",
          acc18["workers"] == 2 and acc18["cpus"] == "0-7"
          and acc18["omp_threads"] == 4 and acc18["split_cpus"] is False, acc18)

    print("[19] 可选顶层 perf 字段（perf/acc/func 资源调度覆盖层）")

    def perf_norm(cand):
        return normalize_perf_resources(normalize_evaluators(dict(cand)))

    def perf_rejects(cand):
        try:
            perf_norm(cand)
            return False
        except SystemExit:
            return True

    # 合成基线：无 evaluators、无 perf，旧扁平键取确定值——断言与生产 config 解耦
    syn = dict(cfg)
    syn.pop("evaluators", None)
    syn.pop("perf", None)
    syn["correctness_workers"], syn["correctness_cpus"] = 3, "4-6"
    syn["correctness_omp_threads"] = 1
    syn["timeout_max_min"], syn["timeout_default_min"] = 30, 15
    syn["queue_max"] = 64

    g0 = perf_norm(syn)
    check("perf 缺省：lane_queue_max 全 None（只受全局 queue_max）",
          g0["lane_queue_max"] == {"timing": None, "correctness": None, "func": None},
          g0["lane_queue_max"])
    check("perf 缺省：perf workers=1/cpus=None/omp=None（timing 不钉核，逐字节兼容）",
          g0["evaluators"]["perf"]["workers"] == 1
          and g0["evaluators"]["perf"]["cpus"] is None
          and g0["evaluators"]["perf"]["omp_threads"] is None, g0["evaluators"]["perf"])
    check("perf 缺省：acc 回落旧扁平键（workers=3/cpus=4-6/omp=1）",
          g0["evaluators"]["acc"]["workers"] == 3
          and g0["evaluators"]["acc"]["cpus"] == "4-6"
          and g0["evaluators"]["acc"]["omp_threads"] == 1, g0["evaluators"]["acc"])
    check("perf 缺省：timing lane_resources=(None,None)",
          lane_resources(g0, "timing") == (None, None), lane_resources(g0, "timing"))

    # 对象形态：核id/并发/队列/omp/执行器/超时 全量覆盖
    p1 = dict(syn)
    p1["perf"] = {
        "perf": {"max_concurrency": 2, "max_queue": 8, "cpu_ids": "0-1", "omp_threads": 2},
        "acc": {"cpu_ids": [12, 13, 14], "max_concurrency": 5, "max_queue": 16,
                "executor": "subprocess", "timeout_min": 6},
        "func": {"cpu_ids": "4,6", "max_concurrency": 1, "max_queue": 4},
    }
    g1 = perf_norm(p1)
    check("cpu_ids 列表→紧凑 taskset 串（acc=[12,13,14]→12-14）",
          g1["evaluators"]["acc"]["cpus"] == "12-14", g1["evaluators"]["acc"]["cpus"])
    check("max_concurrency→workers（perf=2/acc=5/func=1）",
          g1["evaluators"]["perf"]["workers"] == 2
          and g1["evaluators"]["acc"]["workers"] == 5
          and g1["evaluators"]["func"]["workers"] == 1, g1["evaluators"])
    check("max_queue→lane_queue_max（按车道）",
          g1["lane_queue_max"] == {"timing": 8, "correctness": 16, "func": 4},
          g1["lane_queue_max"])
    check("perf 配 cpu_ids/omp→timing 钉核+注 OMP（opt-in）",
          lane_resources(g1, "timing") == ("0-1", 2), lane_resources(g1, "timing"))
    check("executor/timeout_min 归并进 evaluators.acc",
          g1["evaluators"]["acc"]["executor"] == "subprocess"
          and abs(g1["evaluators"]["acc"]["timeout_min"] - 6.0) < 1e-9, g1["evaluators"]["acc"])
    check("func cpu_ids '4,6' 原样保留", g1["evaluators"]["func"]["cpus"] == "4,6",
          g1["evaluators"]["func"]["cpus"])
    check("acc 核数<并发时分核回落整段共享（3核/5人）",
          lane_resources(g1, "correctness", 0, 5) == ("12-14", 1),
          lane_resources(g1, "correctness", 0, 5))

    # perf 字段 cpu_ids 流入已有分核机制（8-15 / 4 worker）
    g5 = perf_norm(dict(syn, perf={"acc": {"cpu_ids": "8-15", "max_concurrency": 4}}))
    check("perf 字段 cpu_ids 流入分核（8-15/4人 → worker1 得 10-11）",
          lane_resources(g5, "correctness", 1, 4) == ("10-11", 1),
          lane_resources(g5, "correctness", 1, 4))

    # 字符串形态（json 字符串）：与对象形态等价
    g2 = perf_norm(dict(syn, perf=json.dumps({"acc": {"cpu_ids": "8-9", "max_concurrency": 2}})))
    check("perf 字段为 json 字符串时自动解析",
          g2["evaluators"]["acc"]["cpus"] == "8-9"
          and g2["evaluators"]["acc"]["workers"] == 2, g2["evaluators"]["acc"])

    # cores 在既有核池上裁剪（无 cpu_ids）：acc 旧池 4-6（3 核）裁到 2 → 4-5
    g3 = perf_norm(dict(syn, perf={"acc": {"cores": 2}}))
    check("cores 无 cpu_ids 时在既有核池裁剪（4-6 取前 2→4-5）",
          g3["evaluators"]["acc"]["cpus"] == "4-5", g3["evaluators"]["acc"]["cpus"])
    check("cores 与 cpu_ids 数目一致时通过",
          perf_norm(dict(syn, perf={"acc": {"cores": 3, "cpu_ids": "12-14"}}))
          ["evaluators"]["acc"]["cpus"] == "12-14")

    # 覆盖优先级：perf 字段 > evaluators
    syn_ev = dict(syn)
    syn_ev["evaluators"] = {"acc": {"enabled": True, "workers": 2, "cpus": "2-3",
                                    "omp_threads": 1}}
    syn_ev["perf"] = {"acc": {"cpu_ids": "8-9", "max_concurrency": 4}}
    g4 = perf_norm(syn_ev)
    check("perf 字段覆盖 evaluators（cpus 2-3→8-9，workers 2→4）",
          g4["evaluators"]["acc"]["cpus"] == "8-9"
          and g4["evaluators"]["acc"]["workers"] == 4, g4["evaluators"]["acc"])

    # 拒绝用例（启动即 SystemExit）
    check("perf 顶层未知键拒绝", perf_rejects(dict(syn, perf={"banana": {}})))
    check("perf.<n> 未知键拒绝", perf_rejects(dict(syn, perf={"acc": {"nice": 0}})))
    check("perf 非对象/非 json 串拒绝", perf_rejects(dict(syn, perf=42)))
    check("perf 串非法 JSON 拒绝", perf_rejects(dict(syn, perf="{not json")))
    check("cpu_ids 非法拒绝", perf_rejects(dict(syn, perf={"acc": {"cpu_ids": "x-y"}})))
    check("cores 与 cpu_ids 数目不一致拒绝",
          perf_rejects(dict(syn, perf={"acc": {"cores": 2, "cpu_ids": "12-14"}})))
    check("cores 超过既有核池拒绝", perf_rejects(dict(syn, perf={"acc": {"cores": 99}})))
    check("perf 给 cores 但 timing 无核池拒绝",
          perf_rejects(dict(syn, perf={"perf": {"cores": 2}})))
    check("max_concurrency<1 拒绝",
          perf_rejects(dict(syn, perf={"acc": {"max_concurrency": 0}})))
    check("max_queue<1 拒绝", perf_rejects(dict(syn, perf={"acc": {"max_queue": 0}})))
    check("executor 非法拒绝", perf_rejects(dict(syn, perf={"acc": {"executor": "magic"}})))
    check("timeout_min 超限拒绝",
          perf_rejects(dict(syn, perf={"acc": {"timeout_min": 9999}})))
    check("omp_threads<1 拒绝", perf_rejects(dict(syn, perf={"acc": {"omp_threads": 0}})))

    # 车道级队列闸（Store.submit）：func max_queue=1 时第二条同车道 429
    cfg_pq = dict(g1)
    cfg_pq["data_dir"] = os.path.join(tmp, "dataPQ")
    os.makedirs(cfg_pq["data_dir"], exist_ok=True)
    cfg_pq["queue_max"] = 64                       # 全局闸放宽，孤立车道闸
    cfg_pq["lane_queue_max"] = dict(g1["lane_queue_max"], func=1)
    store_pq = Store(cfg_pq)
    spq = {"op": "add", "shape": "64", "dtype": "float32", "warmup": 0, "rep": 1,
           "bench": "op", "restore": True, "timeout_min": 1, "lane": "func", "files": []}
    store_pq.submit(dict(spq), dict(spq))
    try:
        store_pq.submit(dict(spq), dict(spq))
        pq_full = False
    except QueueFullError:
        pq_full = True
    check("func max_queue=1 时第 2 条 429（车道闸生效）", pq_full)
    stq = dict(spq)
    stq.pop("lane")                                # 无 lane=timing，不受 func 闸影响
    try:
        store_pq.submit(dict(stq), dict(stq))
        cross_ok = True
    except QueueFullError:
        cross_ok = False
    check("车道闸按车道独立（func 满不挡 timing）", cross_ok)

    # 车道级默认超时（validate_request 取 evaluators.<n>.timeout_min）
    s_to, _, e_to = validate_request(g1, {"op": "add", "shape": "64", "dtype": "float32",
                                          "lane": "correctness"})
    check("correctness 缺省 timeout 取 perf 字段 acc.timeout_min=6",
          e_to is None and abs(s_to["timeout_min"] - 6.0) < 1e-9,
          (e_to, s_to and s_to.get("timeout_min")))
    s_to2, _, e_to2 = validate_request(g1, {"op": "add", "shape": "64", "dtype": "float32"})
    check("timing 无 timeout_min 覆盖→回落全局默认 15",
          e_to2 is None and s_to2["timeout_min"] == 15,
          (e_to2, s_to2 and s_to2.get("timeout_min")))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nselftest: {'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    main()
