#!/usr/bin/env python
"""E1 eval pipeline driver v2 (2026-09-09) — sole evald client (runbook §12).

v2: dual-channel. correctness tickets -> correctness_base (k1-proxy when
configured via runs/E1/.pipeline-config.json), timing + baseline -> K3.
Incumbent comparison guarded per-cluster (ADR-0012): registry
runs/E1/.incumbent-registry.json tracks {host, ref_ms}; on host change the
first result re-baselines (outcome accepted_correctness, no cross-cluster
number ever compared). Poll 404 (proxy ~10min retention) -> bounded resubmit.

Deterministic loop, no LLM. Cycle: healthz gates -> poll inflight -> write
raw.json -> mechanical judgment -> result.json -> drain tickets (correctness
<=3 in flight, same deploy_path serialized (§9.3); timing exclusive) ->
baseline (5/5) last. Writes ONLY under runs/E1/**.
Exit codes: 2=HALT, 3=healthz, 4=submit errors, 7=proxy healthz.

v2.1 (2026-09-10): optional func→acc→perf relay chain, entirely behind
per-tick config switches in .pipeline-config.json (chain_func_acc /
relay_acc_perf / relay_mem_gate_mb / relay_max_per_run, all default off).
Correctness-lane rep=1 coarse timing is trigger-only (>5% same-host compare
via judge) and never enters any report/result path. Design doc:
docs/relay-chain-design.md.
"""
from __future__ import annotations

import faulthandler
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SELF_DIR = Path(__file__).resolve().parent
REPO_DIR = SELF_DIR.parent        # = 仓库根
ROOT = REPO_DIR.parent            # 战役工作区根：runs/ results/ 语义
if os.environ.get("EV_WORKSPACE"):   # 仓已独立于工作区，无法从仓位置推导 → 显式覆盖
    ROOT = Path(os.environ["EV_WORKSPACE"]).expanduser().resolve()
# 评测后端（ADR-0016，B1）：EV_BACKEND 选 BackendProfile；无 backends.py 或未设时
# 恒为 k3 常量——本文件独立部署语义不变。未布点直接死（配置错误不静默回落 k3）。
BACKEND = os.environ.get("EV_BACKEND", "k3")
try:
    from backends import get as _bp_get
except Exception:  # noqa: BLE001 — 板上/最小环境无 clients/backends.py
    _bp_get = None
if _bp_get is not None:
    _bp = _bp_get(BACKEND)
    if not _bp.timing_base:
        raise SystemExit(f"[eval_pipeline] backend {BACKEND!r} 尚未布点（timing_base 空）")
    BASE = _bp.timing_base
else:
    BASE = "http://10.18.60.100:8788"
TOKEN = json.load(open(REPO_DIR / "config.json"))["token"]

RE_SHAPE_OK = re.compile(r"^[0-9]{1,10}(,[0-9]{1,10}){0,5}$")
# 2026-09-10 起正本在 results/e1-ops/（runs/E1/ 旧路径留副本兜底，见
# results/infra/infra-20260910-repartition.md 的归拢）
_MANIFEST_PATH = ROOT / "results" / "e1-ops" / "baseline_manifest.json"
if not _MANIFEST_PATH.is_file():
    raise SystemExit(f"[eval_pipeline] 找不到 baseline_manifest：{_MANIFEST_PATH}\n"
                     f"  （工作区根默认取仓的父目录 {ROOT}；"
                     f"用 EV_WORKSPACE=<含 runs/ 与 results/ 的工作区根> 指定）")
_MANIFEST_RAW = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
MANIFEST = {it["op"]: it for it in _MANIFEST_RAW["jobs"]}

RUN = ROOT / "runs" / "E1"
# 2026-09-10 邻会话汇总迁移：BASELINE→results/baseline-a100（见 results/infra/infra-20260910-repartition.md）
BASELINE_DIR = ROOT / "results" / "baseline-a100"
STATE_FILE = RUN / ".pipeline-state.json"
FLAGS = RUN / ".pipeline-flags.jsonl"
CONFIG_FILE = RUN / ".pipeline-config.json"
REGISTRY_FILE = RUN / ".incumbent-registry.json"
RELAY_TIMED_FILE = RUN / ".relay-timed.json"  # relay 去重：candidate sha256 → 投递时间
CONDITIONS = ["KOPE"]  # 11-era: KOPE-only
# astra 0910_2 C0+C1+C2 各臂（runs/astra_0910_1/<arm>/<op>/tickets 同构扫描）
ASTRA_RUN = ROOT / "runs" / "astra_0910_1"
ASTRA_CONDITIONS = ["full", "kope", "seed_fixed", "zero_fixed", "zero_adaptive",
                    "strategy_only", "full_adaptive", "mask_on", "mask_off", "mask_random",
                    "transfer_on", "target_local"]
# astra 0910_5 夜间同窗三臂（E/H/T × 3 次独立搜索 rep；独立根，作者 2026-09-10 批准）
# r4 = astra_0911_1 §5 P2 全新修复后对照链（evald 加固后、槽位卫生前提下的新 E/H/T 搜索）
# r5 = ds4.1f §3 P0 M 因子配对：M0＝r5/H（alias H, admission=off）、M1＝r5/full（alias full,
#      admission=on）——同一 S0 种子、同 strategy/mask，唯一变量＝目标内记忆回流开关
ASTRA5_RUN = ROOT / "runs" / "astra_0910_5"
ASTRA5_CONDITIONS = [f"r{r}/{a}" for r in (1, 2, 3, 4) for a in ("E", "H", "T")] \
    + ["r5/H", "r5/full"]

# astra 0911_2（exp/实验计划.md §【10】astra_补充验证_0911_2）：F/R/S × 2 次独立搜索；
# 独立状态根 runs/astra_0911_2；registry 分臂键 astra6/<rep>/<cond>/<op>（含 run/rep/条件/op）。
# 另扫 _base（每目标 A100 初始资格显式部署票）与 _heldout（留出 case 正确性票）。
ASTRA6_RUN = ROOT / "runs" / "astra_0911_2"
ASTRA6_CONDITIONS = [f"r{r}/{a}" for r in (1, 2) for a in ("F", "R", "S")] \
    + ["_base", "_heldout"]

# astra 0911_3（exp/实验计划.md §【11】dsv4.1f 补充试验计划 B1/B2，2026-09-11 11:45 批准）：
# B1＝E/H × 12 op × 3 rep 种子增量复现（runs/astra_0911_3，骨架经卫生审计后复用）；
# B2＝KOPE 战役 winner 终点稳定性复测（retime/<op>）。registry 分臂键 astra7/<cond>/<op>，
# 与该根已落盘 63 张 r1 的既有键格式一致。
ASTRA7_RUN = ROOT / "runs" / "astra_0911_3"
ASTRA7_CONDITIONS = [f"r{r}/{a}" for r in (1, 2, 3) for a in ("E", "H")] \
    + ["base", "retime"]

# astra_0911_expand24（exp/实验计划.md §【12】扩面波，2026-09-11 13:3x 批准）：
# 24 新目标 × E/H 两臂 × 1 次独立搜索；registry 键 expand24/<cond>/<op>。
EXPAND_RUN = ROOT / "runs" / "astra_0911_expand24"
EXPAND_CONDITIONS = ["r1/E", "r1/H", "base"]

# astra_0911_expand_next（§【13】第二轮扩面 D 区块，2026-09-11 15:45 批准）：
# 8 新目标 × E/H × 1 次搜索；registry 键 expand_next/<cond>/<op>。
EXPANDN_RUN = ROOT / "runs" / "astra_0911_expand_next"
EXPANDN_CONDITIONS = ["r1/E", "r1/H", "base"]

# astra_0911_expand_late（第三轮扩面 G 区块 4 op，tmp/0911_1650 计划）：
EXPANDL_RUN = ROOT / "runs" / "astra_0911_expand_late"
EXPANDL_CONDITIONS = ["r1/E", "r1/H", "base"]


def path_root(tpath: str):
    """按票相对路径解析其波根（runs/<wave>/...）。2026-09-15 W1 事故：
    expand24/expand_next/astra_0911_3 与 expand_late 共享条件名 r1/E、r1/H、
    base，cond_root() 按条件名先命中 expand_late ⇒ 非 expand_late 票的
    raw/result/台账全部写错根。票路径是唯一真相，一律以它为准。"""
    parts = Path(tpath).parts
    if len(parts) >= 3 and parts[0] == "runs":
        return ROOT / parts[0] / parts[1]
    return None


def entry_root(entry: dict):
    """finalize/relay 用：优先按 ticket_path 解析波根，兜底 cond_root。"""
    r = path_root(entry.get("ticket_path") or "")
    return r if r is not None else cond_root(entry.get("cond"))


def cond_root(cond: str):
    """条件名 → 其工作根（astra 各臂在独立根，KOPE 仍在 runs/E1）。"""
    if cond in EXPANDL_CONDITIONS:
        return EXPANDL_RUN
    if cond in EXPANDN_CONDITIONS:
        return EXPANDN_RUN
    if cond in EXPAND_CONDITIONS:
        return EXPAND_RUN
    if cond in ASTRA7_CONDITIONS:
        return ASTRA7_RUN
    if cond in ASTRA6_CONDITIONS:
        return ASTRA6_RUN
    if cond in ASTRA5_CONDITIONS:
        return ASTRA5_RUN
    return ASTRA_RUN if cond in ASTRA_CONDITIONS else RUN


_WAVE_KEY_PREFIX = {  # 波根目录名 → registry 键前缀（与 cond_key 各分支一致）
    "astra_0911_expand_late": "expand_late",
    "astra_0911_expand_next": "expand_next",
    "astra_0911_expand24": "expand24",
    "astra_0911_3": "astra7",
    "astra_0911_2": "astra6",
    "astra_0910_5": "astra5",
    "astra_0910_1": "astra",
}


def cond_key_from_path(tpath: str, cond: str, op: str) -> str:
    """registry 分臂键：按票路径解析波前缀（2026-09-15 条件名重叠路由事故的
    registry 同源修复——expand 系四波共享 r1/E、r1/H、base，cond_key() 会把
    非 expand_late 链的参照写到 expand_late/<cond>/<op> 键下造成跨波串扰）。"""
    parts = Path(tpath).parts if tpath else ()
    if len(parts) >= 2 and parts[0] == "runs":
        pref = _WAVE_KEY_PREFIX.get(parts[1])
        if pref:
            return f"{pref}/{cond}/{op}"
    return cond_key(cond, op)


def cond_key(cond: str, op: str) -> str:
    """registry 分臂键（2026-09-10 隔离改造）：**一臂更新不得改变另一臂判定**。
    astra6→astra6/<cond>/<op>（cond=r<rep>/<arm>）；astra5→astra5/<rep>/<cond>/<op>；
    astra→astra/<cond>/<op>；E1-KOPE 历史键保留 KOPE/<op>。"""
    if cond in EXPANDL_CONDITIONS:
        return f"expand_late/{cond}/{op}"
    if cond in EXPANDN_CONDITIONS:
        return f"expand_next/{cond}/{op}"
    if cond in EXPAND_CONDITIONS:
        return f"expand24/{cond}/{op}"
    if cond in ASTRA7_CONDITIONS:
        return f"astra7/{cond}/{op}"
    if cond in ASTRA6_CONDITIONS:
        return f"astra6/{cond}/{op}"
    if cond in ASTRA5_CONDITIONS:
        return f"astra5/{cond}/{op}"
    if cond in ASTRA_CONDITIONS:
        return f"astra/{cond}/{op}"
    return f"KOPE/{op}"

def park_ticket_400(tpath, err: str) -> bool:
    """HTTP 400（永久性客户端错误）⇒ 停票：改名 .parked-400 使 scan 跳过。

    返回 True 表示已停票；调用方不再把该错误计入 submit_errors 自杀计数。
    （0911_2 波 09:52 float64 事故教训：坏票卡 FIFO 队头反复 400，
    5 连败自杀令全波停摆 7 分钟——永久错不该触发自杀保护。）
    """
    if "HTTP 400" not in err and "400" not in err.split(":")[0]:
        return False
    try:
        src = Path(tpath)
        dst = src.with_suffix(".ticket.json.parked-400")
        src.rename(dst)
        log(f"PARK_400 {src.name}: {err[:160]}")
        return True
    except Exception:  # noqa: BLE001
        return False


STOCHASTIC = {"bernoulli", "rand", "rand_like", "randn", "randn_like",
              "dropout", "rrelu_with_noise_functional", "multinomial",
              "exponential", "normal", "poisson", "uniform"}
CORR_SLOTS = 9          # k1-proxy: 3 boards x 3 (leave headroom vs 4 workers/board)
TIMING_SLOTS = 3        # K3 timing lane: 1 running + queued prefetch, never idle
                        # 可被 .pipeline-config.json 的 timing_slots 覆盖（双计时车道=6）
CYCLE_S = 20
TERMINAL = ("done", "failed", "timeout", "canceled")
MAX_RESUBMIT = 2        # poll-404 bounded resubmit per ticket


def now_local() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _req(method: str, path: str, body=None, timeout_s=90, base=BASE):
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "X-K3-Token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout_s) as resp:
        return json.loads(resp.read())


def atomic_write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def corr_base() -> str:
    if BACKEND != "k3" and _bp_get is not None:
        # 非 k3：correctness 走 profile（无独立旁路实例时同 timing）；
        # 工作区 correctness_base 是 k3 双实例的部署细节，不做跨后端回落
        return _bp_get(BACKEND).corr_base or BASE
    cfg = load_json(CONFIG_FILE, {})
    return cfg.get("correctness_base", BASE)


def base_label(base: str) -> str:
    # 统一正本在 clients/backends.py（ADR-0016）；无该模块时本地同语义兜底。
    # 未知 URL 旧版一律记 "k1-proxy"，正本改原样串——与历史 host 值不等必触发
    # 重定基（宁可重定基不许假可比）；k3/k3-acc 两簇标签逐字不变。
    try:
        from backends import base_label as _bl
        return _bl(base)
    except Exception:  # noqa: BLE001
        if base == BASE:
            return "k3"
        if base.rstrip("/").endswith(":8791"):
            return "k3-acc"
        return "k1-proxy"


def adapt_job(job: dict) -> dict:
    """k1-proxy nests the backend payload: job['backend']['result']. Flatten
    it into evald-compatible top-level fields (state/result/tails/wait)."""
    b = job.get("backend")
    if not isinstance(b, dict) or job.get("result") is not None:
        return job
    out = dict(job)
    r = b.get("result")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except json.JSONDecodeError:
            r = {"raw": r[:500]}
    out["result"] = r
    logs = b.get("logs") or {}
    for k in ("stdout_tail", "stderr_tail"):
        if out.get(k) is None:
            out[k] = logs.get(k)
    for k in ("error", "queue_wait_s", "duration_s", "ts", "state"):
        if out.get(k) is None:
            v = b.get(k)
            if v is None and isinstance(r, dict):
                v = r.get(k)
            out[k] = v
    return out


def load_state() -> dict:
    s = load_json(STATE_FILE, None)
    if s is None:
        s = {"inflight": {}, "healthz_fail": 0, "submit_errors": 0,
             "proxy_fail": 0, "lost": {}}
    s.setdefault("inflight", {})
    s.setdefault("lost", {})
    s.setdefault("relay_pending", [])  # relay 内存闸挂起的待投计时票（跨重启存活）
    return s


# ---------------------------------------------------------------- tickets ---

def scan_tickets() -> list[dict]:
    """All tickets whose raw.json does not exist yet, FIFO by mtime."""
    out = []
    for root, conds in ((RUN, CONDITIONS), (ASTRA_RUN, ASTRA_CONDITIONS),
                        (ASTRA5_RUN, ASTRA5_CONDITIONS),
                        (ASTRA6_RUN, ASTRA6_CONDITIONS),
                        (ASTRA7_RUN, ASTRA7_CONDITIONS),
                        (EXPAND_RUN, EXPAND_CONDITIONS),
                        (EXPANDN_RUN, EXPANDN_CONDITIONS),
                        (EXPANDL_RUN, EXPANDL_CONDITIONS)):
        for cond in conds:
            for tdir in (root / cond).glob("*/tickets"):
                for tf in tdir.glob("*.ticket.json"):
                    stem = tf.name[: -len(".ticket.json")]
                    if (tdir.parent / "results" / f"{stem}.raw.json").exists():
                        continue
                    try:
                        tk = json.loads(tf.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        continue
                    out.append({"path": tf, "mtime": tf.stat().st_mtime,
                                "cond": cond, "op": tk.get("op") or tdir.parent.name,
                                "stem": stem, "ticket": tk})
    out.sort(key=lambda x: x["mtime"])
    return out


def norm_shape(op: str, shape) -> tuple[str | list[str], str | None]:
    """Normalize the submitted shape into evald's accepted form.

    Two forms (mirrors evald's validate_request):
    - single shape: "64,512,512", or an int list [64, 512, 512] (older
      convention where a list was the dims of one shape)
    - multi shape: a list of strings ["64,512,512", "128,256,256"] — one job,
      N cases, sharing a single import (see docs/evald-优化设计 §1)
    Anything invalid falls back to the manifest shape, then to 64,512,512.
    """
    def ok(s) -> bool:
        return bool(RE_SHAPE_OK.match(str(s or "")))

    if isinstance(shape, (list, tuple)) and shape and all(isinstance(x, str) for x in shape):
        multi = [str(x).strip() for x in shape]
        if all(ok(s) for s in multi):
            return multi, None
    elif isinstance(shape, (list, tuple)):
        try:
            s = ",".join(str(int(x)) for x in shape)
        except (TypeError, ValueError):
            s = ""
        if ok(s):
            return s, None
    else:
        s = str(shape or "")
        if ok(s):
            return s, None
    m = (MANIFEST.get(op) or {}).get("shape") or ""
    if ok(m):
        return str(m), f"ticket shape {shape!r} 不合法，已回退 manifest {m}"
    return "64,512,512", f"ticket shape {shape!r} 不合法（manifest 亦非数字），已回退 64,512,512"


def _split_cases(job: dict) -> tuple[dict, list | None]:
    """Multi-shape job: judge on the primary shape, keep all cases for the record.

    evald returns result.cases[] (no top-level speedup) when several shapes were
    submitted. Downstream (judge, kope-mem, drain) is single-result, so the
    primary shape (cases[0]) drives the verdict while the full list is written
    to raw.json as result_cases. Single-shape jobs pass through untouched —
    their raw.json stays byte-identical.
    """
    r = job.get("result")
    if isinstance(r, dict) and isinstance(r.get("cases"), list) and r["cases"]:
        return r["cases"][0], r["cases"]
    return (r or {}), None


def deploy_key(tk: dict) -> str:
    ph = tk.get("payload_hint") or {}
    return ph.get("deploy_path") or f"flag_gems/ops/{tk.get('op', '?')}.py"


def build_body(t: dict) -> tuple[dict, str | None]:
    tk, ph = t["ticket"], (t["ticket"].get("payload_hint") or {})
    kind = tk.get("kind", "correctness")
    shape, warn = norm_shape(tk.get("op"), tk.get("shape"))
    # 多 shape 是一份整批墙钟（evald 同口径），按 case 数放大；上限与
    # evald MAX_SHAPES×timeout_max_min 对齐
    n_cases = len(shape) if isinstance(shape, list) else 1
    body = {"op": tk["op"], "shape": shape,
            "dtype": tk.get("dtype", "float32"),
            "timeout_min": min((ph.get("timeout_min") or 30) * n_cases, 480),
            "warmup": 5, "rep": 5}
    cand = tk.get("candidate_path")
    if cand and not tk.get("is_base_incumbent"):
        cp = ROOT / cand
        if cp.exists():
            body["files"] = [{"path": deploy_key(tk), "content": cp.read_text(encoding="utf-8")}]
    if kind in ("correctness", "func"):
        body["lane"] = kind
    return body, warn


def lost_key(t: dict) -> str:
    return f"{t['cond']}/{t['op']}/{t['stem']}"


def submit(t: dict, state: dict) -> None:
    body, warn = build_body(t)
    kind = t["ticket"].get("kind", "correctness")
    # func 冒烟与 correctness 同走 8791 旁路（8788 的 acc/func 树结构性不可行，
    # 见 ADR-0012 后 09-09 19:4x 拓扑裁定）
    base = corr_base() if kind in ("correctness", "func") else BASE
    if state["lost"].get(lost_key(t), 0) >= MAX_RESUBMIT:
        with open(FLAGS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_local(), "condition": t["cond"],
                                "op": t["op"], "attempt": t["ticket"].get("attempt"),
                                "outcome": "parked_lost", "note": f"{lost_key(t)} 404×{MAX_RESUBMIT}，已驻留待编排者"}) + "\n")
        log(f"PARK {lost_key(t)}: lost {MAX_RESUBMIT}x, flagging")
        state["lost"][lost_key(t)] = MAX_RESUBMIT + 1  # mark parked (logged once)
        return
    try:
        sub = _req("POST", "/jobs", body, base=base)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}") from e
    jid = sub["job_id"]
    state["inflight"][jid] = {
        "type": "ticket", "cond": t["cond"], "op": t["op"], "stem": t["stem"],
        "kind": kind, "base": base,
        "ticket_path": str(t["path"].relative_to(ROOT)),
        "deploy_key": deploy_key(t["ticket"]),
        "submitted": now_local(), "ticket": t["ticket"], "job_state": sub.get("state", "queued"),
        "shape_sent": body["shape"], "shape_warn": warn,
    }
    if warn:
        log(f"WARN {t['cond']}/{t['op']} {t['stem']}: {warn}")
    sub_root = path_root(str(t["path"].relative_to(ROOT))) or cond_root(t["cond"])
    with open(sub_root / t["cond"] / "evald_jobs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"job_id": jid, "ts": now_local(), "condition": t["cond"],
                            "op": t["op"], "attempt": t["ticket"].get("attempt"),
                            "kind": kind, "state": "submitted", "base": base,
                            "shape_sent": body["shape"], "shape_warn": warn,
                            "ticket_path": str(t["path"])}) + "\n")
    atomic_write(STATE_FILE, state)  # crash-safe: never lose a submitted job_id
    log(f"SUBMIT {t['cond']}/{t['op']} {t['stem']} kind={kind} -> {jid} @{base_label(base)}")


# --------------------------------------------------------------- judgment ---

def judge(tk: dict, job: dict, registry: dict, cur_base: str,
          key_override: str | None = None) -> tuple[str, str, dict]:
    """Mechanical outcome + note + registry mutation hint.
    Incumbent comparison only within same host (ADR-0012).
    key_override：驱动按票路径算出的 registry 键（条件名重叠波必传；None 时
    回退 cond_key——ev.py 单评调用方自担波根歧义）。"""
    op, kind, attempt = tk.get("op"), tk.get("kind", "correctness"), tk.get("attempt", 1)
    st = job.get("state")
    if st == "timeout":
        return "timeout", f"evald 超时（attempt {attempt}），按工单重试语义由优化组决定重开", registry
    if kind == "func":
        # func 冒烟口径：跑通即 pass（server 端 state=done 即 kope_mem.pass_rate=1）；
        # failed 是真结论（编译/执行不通过），不是 infra 故障
        if st == "done":
            return "func_pass", "func 冒烟通过：跑通即 pass（correct 仅参考，无 speedup 结论）", registry
        err = (job.get("error") or "")[:200]
        return "func_fail", f"func 冒烟失败：state={st} err={err or 'n/a'}", registry
    if st != "done":
        err = (job.get("error") or "")[:200]
        return "crash", f"evald state={st} err={err or 'n/a'}；疑似 infra 时由编排者复核", registry
    r = job.get("result") or {}
    correct = r.get("correct")
    if kind != "correctness":
        if correct is False:
            return "timing_failed_correctness", "计时作业 correct=false，数字不可用", registry
        return "timing_recorded", f"timing 单遍 warmup=5/rep=5 记录：gems_ms={r.get('gems_ms')} torch_ms={r.get('torch_ms')} speedup={r.get('speedup')}", registry
    if correct is True:
        g, tms = r.get("gems_ms"), r.get("torch_ms")
        # 2026-09-10 U9-② 分键＋隔离改造：astra 各臂独立参照（此前 KOPE/<op> 跨臂
        # 共享导致后到臂被姊妹臂移参照压成 rejected_slower）；E1-KOPE 历史键保留。
        # 0910_5 §3.1 硬门槛：一臂更新不得改变另一臂判定。
        # 2026-10-07 ADR-0016：backend 轴入键——k3 保持旧格式（424 既有键零漂移），
        # 非 k3 一律 b:<backend>/ 前缀，物理杜绝跨后端共用参照。
        backend = tk.get("backend") or "k3"
        if backend == "k3":
            key = key_override or cond_key(tk.get("cond"), op)
        else:
            from backends import registry_key
            key = key_override or registry_key(tk.get("cond"), op, backend)
        reg = registry.get(key)
        host = base_label(cur_base)
        if isinstance(reg, dict) and reg.get("host") == host and reg.get("ref_ms"):
            ref = reg["ref_ms"]
            if g is not None and g < ref * 0.95:
                entry = {"host": host, "ref_ms": g, "ref_job": job.get("job_id"),
                         "ref_stem": f"r{attempt}", "ts": now_local()}
                if backend != "k3":
                    entry["backend"] = backend   # 溯源（键已带 b: 前缀）
                registry[key] = entry
                return ("accepted_correctness_and_faster",
                        f"correct=true max_abs_err={r.get('max_abs_err')}；gems_ms {g} < {host} 参照 {ref}（>5%，§11 切换通过）", registry)
            if g is not None:
                return ("rejected_slower",
                        f"correct=true 但 gems_ms {g} ≥ {host} 参照 {ref}×0.95（§11 不切换）", registry)
            return "needs_verifier_attention", "correct=true 但缺 gems_ms，无法机械比较", registry
        # cold start or cross-cluster cutover: re-baseline, no cross-cluster compare
        entry = {"host": host, "ref_ms": g, "ref_job": job.get("job_id"),
                 "ref_stem": f"r{attempt}", "ts": now_local(), "rebaselined": True}
        if backend != "k3":
            entry["backend"] = backend
        registry[key] = entry
        why = "跨簇切换重定基（ADR-0012：不与旧簇数字比较）" if isinstance(reg, dict) else "本 op 首个可用参照"
        return ("accepted_correctness",
                f"correct=true max_abs_err={r.get('max_abs_err')}；{why}，本结果 gems_ms={g} 记为 {host} 新参照", registry)
    if correct is False:
        return "rejected_correctness", f"correct=false max_abs_err={r.get('max_abs_err')}", registry
    # correct is None/missing
    if op in STOCHASTIC:
        return "needs_stats_check", "stochastic 族无逐元素比对，统计校验待编排者裁定", registry
    if int(attempt or 1) >= 2:
        return "suspended_unverifiable", "§3.2a：correct≡null 且无合同统计，attempts≥2 挂起", registry
    return "unverifiable_retry", "§3.2a：correct≡null 且 attempts<2，可开 retry 工单（FIFO 尾）", registry


MECHANICAL_OK = {"accepted_correctness_and_faster", "accepted_correctness",
                 "rejected_correctness", "rejected_slower", "timeout",
                 "timing_recorded", "func_pass", "func_fail"}


def finalize_ticket(entry: dict, job: dict, state: dict, registry: dict) -> None:
    tk = entry["ticket"]
    op, stem, cond = entry["op"], entry["stem"], entry["cond"]
    base_root = entry_root(entry)   # 2026-09-15：按票路径解析（条件名重叠路由事故修复）
    res_dir = base_root / cond / op / "results"
    res_dir.mkdir(parents=True, exist_ok=True)
    if (res_dir / f"{stem}.raw.json").exists():
        log(f"SKIP-DUP {cond}/{op} {stem}: raw already exists (duplicate submit)")
        return
    r, cases = _split_cases(job)
    # judge 是单结果语义：多 shape 时用主 shape（cases[0]）判定；
    # backend 穿线（ADR-0016）：票自带优先，缺省继承进程级 EV_BACKEND——
    # 路由（BASE）与 registry 键命名空间必须同后端
    tk_with_cond = dict(tk, cond=entry.get("cond", tk.get("cond")),
                        backend=tk.get("backend") or BACKEND)
    job_j = dict(job, result=r) if cases is not None else job
    _key_o = cond_key_from_path(entry.get("ticket_path") or "", cond, op)
    outcome, note, registry = judge(tk_with_cond, job_j, registry,
                                    entry.get("base", BASE), key_override=_key_o)
    atomic_write(REGISTRY_FILE, registry)
    raw = {
        "schema": "raw-v1", "writer": "pipeline-v1", "condition": cond,
        "kind": tk.get("kind", "correctness"), "op": op,
        "backend": tk_with_cond["backend"],
        "shape": tk.get("shape"), "dtype": tk.get("dtype"),
        "attempt": tk.get("attempt"),
        "candidate_sha256": tk.get("candidate_sha256"),
        "candidate_path": tk.get("candidate_path"),
        "incumbent_timing": tk.get("incumbent_timing"),
        "is_base_incumbent": tk.get("is_base_incumbent"),
        "payload_hint": tk.get("payload_hint"), "gen": tk.get("gen"),
        "job_id": entry["job_id"], "state": job.get("state"),
        "evald_ts": job.get("ts") or now_local(),
        "evald_host": base_label(entry.get("base", BASE)),
        "queue_wait_s": job.get("queue_wait_s"), "duration_s": job.get("duration_s"),
        "shape_sent": entry.get("shape_sent"), "shape_warn": entry.get("shape_warn"),
        "result": r,
        "kope_mem": {"speedup": r.get("speedup"),
                     "pass_rate": 1.0 if r.get("correct") is True else 0.0},
        "stdout_tail": job.get("stdout_tail"), "stderr_tail": job.get("stderr_tail"),
        "error": job.get("error"),
    }
    if cases is not None:
        raw["result_cases"] = cases      # 附加：非主 shape 的结果，判定不读它
    atomic_write(res_dir / f"{stem}.raw.json", raw)
    result = {
        "job_id": entry["job_id"], "state": job.get("state"),
        "evald_ts": raw["evald_ts"], "evald_host": raw["evald_host"],
        "condition": cond, "op": op,
        "attempt": tk.get("attempt"), "kind": tk.get("kind", "correctness"),
        "candidate_sha256": tk.get("candidate_sha256"),
        "candidate_path": tk.get("candidate_path"),
        "result": r, "stdout_tail": job.get("stdout_tail"),
        "stderr_tail": job.get("stderr_tail"), "error": job.get("error"),
        "queue_wait_s": job.get("queue_wait_s"), "duration_s": job.get("duration_s"),
        "outcome": outcome, "retry_notes": note, "semantics_note": note,
        "judge": "pipeline-v1-mechanical",
    }
    atomic_write(res_dir / f"{stem}.result.json", result)
    # B2（ADR-0017）：统一结果视图随写；无 result_schema（板上最小部署）显式提示不静默
    try:
        from result_schema import to_result_v2
        atomic_write(res_dir / f"{stem}.result-v2.json", to_result_v2(
            job_j, backend=BACKEND, host_tag=raw["evald_host"],
            lane={"correctness": "correctness", "func": "func"}.get(
                tk.get("kind", "correctness"), "timing"),
            meta={"condition": cond, "attempt": tk.get("attempt"), "stem": stem,
                  "kind": tk.get("kind", "correctness"), "outcome": outcome,
                  "note": note, "candidate_sha256": tk.get("candidate_sha256"),
                  "candidate_path": tk.get("candidate_path")}))
    except ImportError:
        log(f"result-v2 未写（无 result_schema）：{cond}/{op} {stem}")
    if outcome == "unverifiable_retry":
        # W0-4（2026-09-15）：单文件覆盖会丢更早的 retry 事件——改 append-only
        # JSONL 台账留全史；pending_retry.json 保留为"最新状态"快照（兼容旧语义）
        pr = {"op": op, "attempt": tk.get("attempt"), "reason": note,
              "ts": now_local(), "cond": cond, "stem": stem,
              "job_id": entry["job_id"]}
        with open(base_root / cond / op / "tickets" / "pending_retry.jsonl",
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(pr, ensure_ascii=False) + "\n")
        atomic_write(base_root / cond / op / "tickets" / "pending_retry.json", pr)
    if outcome not in MECHANICAL_OK:
        with open(FLAGS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_local(), "condition": cond, "op": op,
                                "attempt": tk.get("attempt"), "outcome": outcome,
                                "job_id": entry["job_id"], "state": job.get("state")}) + "\n")
    with open(base_root / cond / "evald_jobs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"job_id": entry["job_id"], "ts": now_local(),
                            "condition": cond, "op": op,
                            "attempt": tk.get("attempt"),
                            "kind": tk.get("kind"), "state": job.get("state"),
                            "duration_s": job.get("duration_s"),
                            "outcome": outcome, "base": entry.get("base", BASE),
                            "ticket_path": entry["ticket_path"]}) + "\n")
    log(f"FINAL {cond}/{op} {stem}: {job.get('state')} -> {outcome} @{raw['evald_host']}")
    state["deploy_paths"].pop(entry.get("deploy_key") or "", None)


# ------------------------------------------------- func→acc→perf 自动接力 ---
#
# 目标链路（全部挂配置开关，默认关闭；设计正本 docs/relay-chain-design.md）：
#   func_pass --chain_func_acc--> acc 票（corr_base 车道）
#     --correct 且粗计时快于现任>5%--> relay_acc_perf --> 计时票（BASE 8788，热缓存）
# 口径红线：X100 acc 车道返回的 rep=1 粗计时只许做「触发/排序」比较（沿用 judge 的
# 同簇 >5% 结论），绝不写入任何 result/报告/入库路径；计时正本只在 BASE（A100，
# warmup=5/rep=5）产出。依据 ADR-0012 / 实验环境.md §3、§4.1。

PSSH = REPO_DIR / "ops" / "pssh.py"     # 内存闸只读探测通道（paramiko，见 实验环境.md §0）
MEM_GATE_HOST = "Home-SpacemitK3"
_RELAY_RUNTIME = {  # 仅本进程生命周期，不落盘（重启即重新计预算/探测节拍）
    "perf_submitted": 0,    # 本次运行已自动投出的 relay 计时票数
    "mem_fail_streak": 0,   # 内存闸 SSH 探测连续失败次数（成功即清零）
    "probe_at": 0.0,        # 上次探测时刻（CYCLE_S 内复用，一 tick 至多一次 SSH）
    "probe_mb": None,       # 上次探测结果（MB）；None=探测失败
    "cap_flagged": False,   # 超上限 flags 只注记一次
}


def _relay_flag(cond: str, op: str, outcome: str, note: str) -> None:
    """relay 专用 flags 追加（与既有 flags 写法同款）。"""
    with open(FLAGS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_local(), "condition": cond, "op": op,
                            "outcome": outcome, "note": note}) + "\n")


def _pssh_python() -> str:
    """跑 pssh.py 的解释器（同 ops/precompile.py：env 可覆盖，默认当前解释器）。"""
    return os.environ.get("KOPE_PSSH_PYTHON") or sys.executable


def mem_available_mb() -> int | None:
    """经 ops/pssh.py 只读查 K3 板 MemAvailable（kB→MB）；任何异常返回 None。

    None = 探测失败，由 _mem_gate_ok 按 fail-closed 处理（本 tick 挂起）。
    """
    cmd = [_pssh_python(), str(PSSH), MEM_GATE_HOST,
           "grep MemAvailable /proc/meminfo", "--timeout", "30"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90)
    except Exception:  # noqa: BLE001  超时/解释器缺 paramiko 等一律 fail-closed
        return None
    m = re.search(r"MemAvailable:\s+(\d+)\s+kB", (p.stdout or "") + (p.stderr or ""))
    return (int(m.group(1)) // 1024) if m else None


def _mem_gate_ok(thr_mb: int) -> tuple[bool, str]:
    """内存闸：MemAvailable >= thr_mb 才放行 relay 投票。

    探测结果按 CYCLE_S 缓存（一 tick 至多一次 SSH，多条挂起票共享一次探测）。
    SSH 异常 fail-closed（挂起）；连续 3 次失败后降级放行并注记 flags（防 SSH
    抖动卡死接力；探测恢复成功即自动收回降级）。
    """
    c = _RELAY_RUNTIME
    now = time.time()
    if not c["probe_at"] or now - c["probe_at"] >= CYCLE_S:
        c["probe_at"], c["probe_mb"] = now, mem_available_mb()
        if c["probe_mb"] is None:
            c["mem_fail_streak"] += 1
        else:
            c["mem_fail_streak"] = 0
        if c["mem_fail_streak"] == 3:
            _relay_flag("relay", "-", "relay_mem_gate_degraded",
                        f"SSH 探测连续失败 {c['mem_fail_streak']} 次，内存闸降级放行（防卡死接力）")
    mb = c["probe_mb"]
    if mb is None:
        if c["mem_fail_streak"] >= 3:
            return True, "SSH 探测连续失败，降级放行"
        return False, "SSH 探测失败（fail-closed 挂起）"
    if mb < thr_mb:
        return False, f"MemAvailable {mb}MB < 阈值 {thr_mb}MB"
    return True, f"MemAvailable {mb}MB"


def _relay_dedup_exists(root: Path, cond: str, op: str, stem: str, state: dict) -> bool:
    """同 lost_key 的接电工单是否已存在（工单文件在即含已完结；含在飞 inflight）。"""
    tpath = root / cond / op / "tickets" / f"{stem}.ticket.json"
    if tpath.exists():
        return True
    rel = str(tpath.relative_to(ROOT))
    return any(e.get("ticket_path") == rel for e in state["inflight"].values()
               if e["type"] == "ticket")


def _synth_relay_ticket(root: Path, cond: str, op: str, stem: str, tk: dict, note: str) -> str:
    """把合成工单落盘到常规 tickets 目录——后续由 scan_tickets 扫到、走 submit
    现有路径（车道路由/deploy_path 避让/path_lock 全继承，不另起 HTTP 调用）。"""
    tpath = root / cond / op / "tickets" / f"{stem}.ticket.json"
    tpath.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(tpath, tk)
    log(f"RELAY {cond}/{op}: 合成工单 {stem}.ticket.json（{note}）")
    return str(tpath.relative_to(ROOT))


def _acc_relay_from_func(entry: dict, state: dict) -> None:
    """chain_func_acc：func 票终态 func_pass → 为同一
    (cond/op/stem/shape/dtype/candidate_path) 合成 acc 票（kind=correctness），
    下次 scan 起走进 corr 车道（CORR_SLOTS / 同路径避让照常约束）。"""
    cond, op, stem = entry["cond"], entry["op"], entry["stem"]
    root = entry_root(entry)
    acc_stem = f"{stem}-acc"
    if _relay_dedup_exists(root, cond, op, acc_stem, state):
        return  # 去重：scan_tickets/infl_tickets 里已有同 lost_key 的 acc 票
    src = entry.get("ticket") or {}
    tk = {k: src[k] for k in ("op", "shape", "dtype", "attempt", "candidate_sha256",
                              "candidate_path", "incumbent_timing",
                              "is_base_incumbent", "gen") if k in src}
    tk["kind"] = "correctness"
    ph = dict(src.get("payload_hint") or {})
    ph["notes"] = ((ph.get("notes") or "") + " | " if ph.get("notes") else "") + \
        "relay: func_pass 自动接 acc（chain_func_acc）"
    tk["payload_hint"] = ph
    _synth_relay_ticket(root, cond, op, acc_stem, tk, "func_pass→acc")
    _relay_flag(cond, op, "relay_acc_synth",
                f"{cond}/{op}/{stem} func_pass → 自动合成 acc 工单 {acc_stem}")


def _perf_ticket_from(src: dict) -> dict:
    """与现行 timing-final 工单同构；files 由 build_body 按 candidate_path 照常
    校验携带（相同 files 语义），submit 按 kind 路由投 BASE（8788，热缓存）。"""
    ph = dict(src.get("payload_hint") or {})
    ph["notes"] = "relay: acc 车道 correct 且粗计时快于现任>5%，自动接 perf（relay_acc_perf；" \
                  "粗计时仅触发比较，不入报告/入库——红线见 实验环境.md §3/§4.1）"
    return {"kind": "timing-final", "op": src.get("op"), "shape": src.get("shape"),
            "dtype": src.get("dtype", "float32"), "attempt": src.get("attempt", 1),
            "candidate_sha256": src.get("candidate_sha256"),
            "candidate_path": src.get("candidate_path"),
            "incumbent_timing": None, "is_base_incumbent": False,
            "payload_hint": ph}


def _perf_relay_ent(entry: dict, key: str) -> dict:
    """挂起队列元素：足够在后续 tick 独立合成计时票的最小信息集。"""
    return {"key": key, "cond": entry["cond"], "op": entry["op"],
            "stem": entry["stem"], "ticket": entry.get("ticket") or {},
            "ticket_path": entry.get("ticket_path"),
            "src_job": entry.get("job_id"), "queued": now_local()}


def _perf_relay_submit(ent: dict, state: dict, relay_timed: dict) -> None:
    """落盘计时票 + 记去重 + 计数 + flags（直接触发与挂起冲刷两条路径共用）。"""
    cond, op, stem = ent["cond"], ent["op"], ent["stem"]
    root = entry_root(ent)
    perf_stem = f"{stem}-perf"
    rel = _synth_relay_ticket(root, cond, op, perf_stem, _perf_ticket_from(ent["ticket"]),
                              "acc→perf 热缓存")
    relay_timed[ent["key"]] = {"ts": now_local(), "src": f"{cond}/{op}/{stem}",
                               "src_job": ent.get("src_job"), "ticket": rel}
    atomic_write(RELAY_TIMED_FILE, relay_timed)
    _RELAY_RUNTIME["perf_submitted"] += 1
    _relay_flag(cond, op, "relay_perf_synth",
                f"{cond}/{op}/{stem} acc 通过且粗计时快于现任>5% → 自动合成计时票 "
                f"{perf_stem}（本次运行第 {_RELAY_RUNTIME['perf_submitted']} 张）")


def _perf_relay_from_acc(entry: dict, state: dict, relay_timed: dict, cfg: dict) -> None:
    """relay_acc_perf：correctness 票 done 且 result.correct==true。机械判定直接
    委托 judge 的 accepted_correctness_and_faster——judge 已在同一 registry 快照上
    做过同簇（host 键区分 k3-acc/k3）粗计时 >5% 比较，此处不重复比较、不重算
    （judge 切换后 ref:=g，重算永远不满足）。过上限/内存闸后合成计时票投 BASE。"""
    cond, op, stem = entry["cond"], entry["op"], entry["stem"]
    src = entry.get("ticket") or {}
    if src.get("is_base_incumbent"):
        _relay_flag(cond, op, "relay_skip_base",
                    f"{cond}/{op}/{stem} 为现任基底票，不自动接 perf")
        return
    cand = src.get("candidate_path")
    if not cand or not (ROOT / cand).exists():
        _relay_flag(cond, op, "relay_skip_no_candidate",
                    f"{cond}/{op}/{stem} candidate_path 缺失/不存在（{cand!r}），"
                    f"防计时空跑，不接 perf")
        return
    key = f"{op}/{src.get('candidate_sha256') or f'stem:{stem}'}"
    if key in relay_timed:
        return  # 去重：.relay-timed.json 已有同候选（同候选只自动投一次）
    if any(p.get("key") == key for p in state.get("relay_pending") or []):
        return  # 去重：已因内存闸挂起待投
    cap = int(cfg.get("relay_max_per_run") or 10)
    if _RELAY_RUNTIME["perf_submitted"] >= cap:
        if not _RELAY_RUNTIME["cap_flagged"]:
            _relay_flag(cond, op, "relay_cap_reached",
                        f"本次运行 relay 计时票已达上限 {cap}，停投"
                        f"（新触发不排队，如需补计时人工开票）")
            _RELAY_RUNTIME["cap_flagged"] = True
        return
    thr = int(cfg.get("relay_mem_gate_mb") or 3072)
    ok, why = _mem_gate_ok(thr)
    if not ok:
        state.setdefault("relay_pending", []).append(_perf_relay_ent(entry, key))
        _relay_flag(cond, op, "relay_mem_hold",
                    f"{cond}/{op}/{stem} perf 接力挂起，下 tick 重试：{why}")
        return
    _perf_relay_submit(_perf_relay_ent(entry, key), state, relay_timed)


def flush_relay_pending(state: dict, relay_timed: dict) -> None:
    """每 tick 冲刷内存闸挂起的 relay 计时票（开关关闭时不冲刷，队列原样保留）。"""
    pend = state.get("relay_pending") or []
    if not pend:
        return
    cfg = load_json(CONFIG_FILE, {})
    if not bool(cfg.get("relay_acc_perf")):
        return
    cap = int(cfg.get("relay_max_per_run") or 10)
    thr = int(cfg.get("relay_mem_gate_mb") or 3072)
    still: list[dict] = []
    for ent in pend:
        if _RELAY_RUNTIME["perf_submitted"] >= cap:
            if not _RELAY_RUNTIME["cap_flagged"]:
                _relay_flag(ent["cond"], ent["op"], "relay_cap_reached",
                            f"本次运行 relay 计时票已达上限 {cap}，挂起队列保留待下次运行")
                _RELAY_RUNTIME["cap_flagged"] = True
            still.append(ent)
            continue
        ok, why = _mem_gate_ok(thr)
        if not ok:
            _relay_flag(ent["cond"], ent["op"], "relay_mem_hold",
                        f"{ent['cond']}/{ent['op']}/{ent['stem']} perf 接力挂起，下 tick 重试：{why}")
            still.append(ent)
            continue
        cand = (ent.get("ticket") or {}).get("candidate_path")
        if not cand or not (ROOT / cand).exists():
            _relay_flag(ent["cond"], ent["op"], "relay_drop_no_candidate",
                        f"{ent['cond']}/{ent['op']}/{ent['stem']} 候选文件已消失，丢弃挂起票")
            continue
        _perf_relay_submit(ent, state, relay_timed)
    state["relay_pending"] = still


def relay_hook(entry: dict, state: dict, relay_timed: dict) -> None:
    """工单终态落盘后的接力钩子（全部挂配置开关，默认关闭，逐 tick 读 CONFIG_FILE，
    沿用 correctness_paused 的读取姿势）。

    口径红线：只消费作业 result 里的 rep=1 粗计时做触发/排序比较（沿用 judge 的
    同簇 >5% 结论，本钩子不读数值、不重复比较）；粗计时绝不写入任何
    result/报告/入库路径。
    """
    cfg = load_json(CONFIG_FILE, {})
    kind = entry.get("kind", "correctness")
    res = load_json(entry_root(entry) / entry["cond"] / entry["op"] / "results"
                    / f"{entry['stem']}.result.json", None) or {}
    outcome = res.get("outcome")
    if kind == "func" and outcome == "func_pass" and bool(cfg.get("chain_func_acc")):
        _acc_relay_from_func(entry, state)
    if (kind == "correctness" and outcome == "accepted_correctness_and_faster"
            and bool(cfg.get("relay_acc_perf"))):
        _perf_relay_from_acc(entry, state, relay_timed, cfg)


# --------------------------------------------------------------- baseline ---

def baseline_todo(inflight_ops: set | None = None) -> list[dict]:
    mf = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    skip = set(inflight_ops or ())
    out = []
    for it in mf["jobs"]:
        if it["op"] in skip:
            continue
        if not (BASELINE_DIR / f"{it['op']}.baseline.json").exists():
            out.append(it)
    return out


def submit_baseline(item: dict, state: dict) -> None:
    shape, warn = norm_shape(item["op"], item.get("shape"))
    body = {"op": item["op"], "shape": shape,
            "dtype": item.get("dtype", "float32"),
            "warmup": 5, "rep": 5, "timeout_min": 20}
    try:
        sub = _req("POST", "/jobs", body, base=BASE)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}") from e
    jid = sub["job_id"]
    state["inflight"][jid] = {
        "type": "baseline", "op": item["op"], "shape": shape,
        "dtype": body["dtype"], "submitted": now_local(), "base": BASE,
        "job_state": sub.get("state", "queued"), "shape_warn": warn,
    }
    if warn:
        log(f"WARN baseline {item['op']}: {warn}")
    with open(BASELINE_DIR / "_jobs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"job_id": jid, "ts": now_local(), "op": item["op"],
                            "state": "submitted", "params": {"warmup": 5, "rep": 5}}) + "\n")
    atomic_write(STATE_FILE, state)
    log(f"SUBMIT baseline {item['op']} -> {jid} @k3")


def finalize_baseline(entry: dict, job: dict, state: dict) -> None:
    r, cases = _split_cases(job)
    dst = BASELINE_DIR / f"{entry['op']}.baseline.json"
    if dst.exists():
        log(f"SKIP-DUP baseline {entry['op']}: file exists (duplicate submit)")
        return
    rec = {
        "op": entry["op"], "shape": entry.get("shape"), "dtype": entry.get("dtype"),
        "kind": "baseline", "writer": "pipeline-v1",
        "submit_params": {"warmup": 5, "rep": 5, "cluster": "A100"},
        "job_id": entry["job_id"], "state": job.get("state"),
        "evald_ts": job.get("ts") or now_local(),
        "queue_wait_s": job.get("queue_wait_s"), "duration_s": job.get("duration_s"),
        "result": r,
        "stdout_tail": job.get("stdout_tail"), "stderr_tail": job.get("stderr_tail"),
        "error": job.get("error"),
    }
    if cases is not None:
        rec["result_cases"] = cases
    atomic_write(BASELINE_DIR / f"{entry['op']}.baseline.json", rec)
    with open(BASELINE_DIR / "_jobs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"job_id": entry["job_id"], "ts": now_local(),
                            "op": entry["op"], "state": job.get("state"),
                            "speedup": r.get("speedup"), "correct": r.get("correct")}) + "\n")
    log(f"BASELINE {entry['op']}: {job.get('state')} speedup={r.get('speedup')}")
    if job.get("state") != "done" or r.get("correct") is not True:
        with open(FLAGS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_local(), "condition": "baseline",
                                "op": entry["op"], "outcome": "baseline_anomaly",
                                "job_id": entry["job_id"], "state": job.get("state")}) + "\n")


# ------------------------------------------------------------------- main ---

def healthz_ok(base: str) -> bool:
    h = _req("GET", "/healthz", timeout_s=15, base=base)
    # K3-style (8788 主/8791 acc 旁路) 回复 {"ok": true}；proxy 风格才有 backends
    if base == BASE or "backends" not in h:
        return h.get("ok") is True
    healthy = [b for b in (h.get("backends") or []) if b.get("healthy")]
    # proxy failover tolerates single-board loss (its round_robin + board-swap
    # retry); only treat as outage when NO backend remains
    return bool(healthy)


def main() -> None:
    (BASELINE_DIR).mkdir(parents=True, exist_ok=True)
    state = load_state()
    state.setdefault("deploy_paths", {})
    registry = load_json(REGISTRY_FILE, {})
    relay_timed = load_json(RELAY_TIMED_FILE, {})
    log(f"pipeline-v2 start: {len(state['inflight'])} inflight recovered, "
        f"correctness_base={corr_base()}")
    _rc = load_json(CONFIG_FILE, {})
    log(f"relay chain: chain_func_acc={bool(_rc.get('chain_func_acc'))} "
        f"relay_acc_perf={bool(_rc.get('relay_acc_perf'))} "
        f"mem_gate={int(_rc.get('relay_mem_gate_mb') or 3072)}MB "
        f"max_per_run={int(_rc.get('relay_max_per_run') or 10)} "
        f"timed={len(relay_timed)} pending={len(state.get('relay_pending') or [])}")
    # W0-3（2026-09-15）周期看门狗：16:38 挂死模式（进程活但周期静默 10+ 分钟）
    # 的可观测兜底。每周期武装 faulthandler——周期超过 watchdog_cycle_s（默认 600s）
    # 时把全部线程栈写到 .driver-watchdog.log 后退出（退出码 1）；状态文件 crash-safe，
    # evald spool 跨重启存活，重启驱动即恢复。不覆盖 time.sleep（在武装范围外）。
    wd_s = int(load_json(CONFIG_FILE, {}).get("watchdog_cycle_s") or 600)
    wd_fh = open(RUN / ".driver-watchdog.log", "a", encoding="utf-8")
    wd_fh.write(f"[{now_local()}] driver start, watchdog_cycle_s={wd_s}\n")
    wd_fh.flush()
    # SystemExit（HALT/healthz/submit 自杀）路径也要解除武装：atexit 幂等取消；
    # os._exit 崩溃路径不经过 atexit——那正是要看 dump 的场景
    import atexit
    atexit.register(faulthandler.cancel_dump_traceback_later)
    while True:
        faulthandler.dump_traceback_later(wd_s, file=wd_fh, exit=True)
        _t_cycle = time.time()
        # gates
        if (RUN / "HALT").exists():
            log("HALT file present -> exit 2")
            raise SystemExit(2)
        try:
            if healthz_ok(BASE):
                state["healthz_fail"] = 0
            else:
                raise RuntimeError("healthz body not ok")
        except Exception as e:  # noqa: BLE001
            state["healthz_fail"] += 1
            log(f"healthz fail #{state['healthz_fail']}: {e}")
            if state["healthz_fail"] >= 3:
                raise SystemExit(3)
        cb = corr_base()
        # K1 fleet offline 2026-09-09: skip the proxy gate while correctness
        # submissions are paused, so proxy death can't take baseline/timing down
        if cb != BASE and not bool(load_json(CONFIG_FILE, {}).get("correctness_paused")):
            try:
                if not healthz_ok(cb):
                    raise RuntimeError("proxy backends not all healthy")
                state["proxy_fail"] = 0
            except Exception as e:  # noqa: BLE001
                state["proxy_fail"] = state.get("proxy_fail", 0) + 1
                log(f"proxy healthz fail #{state['proxy_fail']}: {e}")
                if state["proxy_fail"] >= 3:
                    raise SystemExit(7)
        # poll inflight
        for jid in list(state["inflight"]):
            entry = state["inflight"][jid]
            entry["job_id"] = jid
            base = entry.get("base", BASE)
            try:
                job = _req("GET", f"/jobs/{jid}", timeout_s=30, base=base)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    key = f"{entry['cond']}/{entry['op']}/{entry['stem']}" if entry["type"] == "ticket" else f"baseline/{entry['op']}"
                    n = state["lost"].get(key, 0) + 1
                    state["lost"][key] = n
                    log(f"LOST {jid} ({key}): 404, resubmit #{n}")
                    state["inflight"].pop(jid, None)
                    continue
                log(f"poll {jid} error: {e}")
                continue
            except Exception as e:  # noqa: BLE001
                log(f"poll {jid} error: {e}")
                continue
            st = job.get("state")
            entry["job_state"] = st
            if st in TERMINAL:
                job = adapt_job(job)
                if entry["type"] == "ticket":
                    finalize_ticket(entry, job, state, registry)
                    relay_hook(entry, state, relay_timed)  # func→acc→perf 接力（默认关）
                else:
                    finalize_baseline(entry, job, state)
                state["inflight"].pop(jid, None)
        # relay 挂起队列冲刷（内存闸放行后补投计时票；开关关闭时队列原样保留）
        flush_relay_pending(state, relay_timed)
        # scan tickets
        try:
            pend = scan_tickets()
        except Exception as e:  # noqa: BLE001
            pend = []
            log(f"scan error: {e}")
        corr = [t for t in pend if t["ticket"].get("kind", "correctness") in ("correctness", "func")]
        tim = [t for t in pend if t["ticket"].get("kind", "correctness") not in ("correctness", "func")]
        corr_infl = sum(1 for e in state["inflight"].values()
                        if e["type"] == "ticket" and e["kind"] in ("correctness", "func"))
        tim_infl = sum(1 for e in state["inflight"].values()
                       if e["type"] == "ticket" and e["kind"] != "correctness")
        base_infl = sum(1 for e in state["inflight"].values() if e["type"] == "baseline")
        busy_paths = {e["deploy_key"] for e in state["inflight"].values()
                      if e.get("deploy_key")}
        # 同主机、同 deploy_path 的在飞作业：双计时车道下两个作业写同一个
        # flag_gems/ops/<op>.py 会错配候选（服务端 path_lock 是兜底，客户端先避让
        # 才不白占车道）。8791 与 8788 是同一块 K3 板（不同进程/核分区），文件系统
        # 共享——K3 侧作业按"是否落在本板"归组，与 230:8790（k1-proxy，已消亡）区分。
        corr_base_url = corr_base()
        k3_busy_paths = {e["deploy_key"] for e in state["inflight"].values()
                         if e.get("deploy_key")
                         and e.get("base", BASE).rstrip("/") in {BASE.rstrip("/"), corr_base_url.rstrip("/")}}
        # 已在飞工单（不分主机）：raw.json 未落盘前工单会被 scan_tickets 反复扫到，
        # 没有这道门禁就会每周期重投同一张工单（TIMING_SLOTS 越大越严重）
        infl_tickets = {e.get("ticket_path") for e in state["inflight"].values()
                        if e["type"] == "ticket" and e.get("ticket_path")}
        # submit correctness (FIFO, 9.3 same-path serialize)
        # baseline-only mode gate: correctness_paused=true skips new
        # correctness submissions entirely (in-flight still finalize)
        corr_paused = bool(load_json(CONFIG_FILE, {}).get("correctness_paused"))
        base_paused = bool(load_json(CONFIG_FILE, {}).get("baseline_paused"))
        for t in ([] if corr_paused else corr):
            if corr_infl >= CORR_SLOTS:
                break
            dk = deploy_key(t["ticket"])
            if dk in busy_paths:
                continue
            if state["lost"].get(lost_key(t), 0) > MAX_RESUBMIT:
                continue  # parked
            try:
                submit(t, state)
                state["deploy_paths"][dk] = {"job": t["op"], "since": now_local()}
                busy_paths.add(dk)
                corr_infl += 1
                state["submit_errors"] = 0
            except Exception as e:  # noqa: BLE001
                if "HTTP 400" in str(e):
                    park_ticket_400(t["path"], str(e))
                    log(f"submit_400_nopark {t['op']}: {str(e)[:120]}")
                    continue
                state["submit_errors"] += 1
                log(f"submit error {t['op']}: {e}")
                if state["submit_errors"] >= 5:
                    raise SystemExit(4)
        # timing lane: keep timing_slots jobs in flight (running + queued prefetch
        # on evald's worker(s)) so the lane never idles between driver cycles.
        # Timing-final tickets take priority over baseline.
        timing_slots = int(load_json(CONFIG_FILE, {}).get("timing_slots") or TIMING_SLOTS)
        timing_infl = tim_infl + base_infl
        while timing_infl < timing_slots:
            acted = False
            for t in tim:
                dk = deploy_key(t["ticket"])
                rel = str(t["path"].relative_to(ROOT))
                if rel in infl_tickets or dk in k3_busy_paths:
                    continue        # 已在飞 / 同主机同部署路径已占用
                if state["lost"].get(lost_key(t), 0) > MAX_RESUBMIT:
                    continue        # parked（与 correctness 同款语义）
                try:
                    submit(t, state)
                    state["deploy_paths"][dk] = {"job": t["op"], "since": now_local()}
                    k3_busy_paths.add(dk)
                    infl_tickets.add(rel)
                    timing_infl += 1
                    acted = True
                except Exception as e:  # noqa: BLE001
                    if "HTTP 400" in str(e):
                        park_ticket_400(t["path"], str(e))
                        log(f"submit_400_nopark {t['op']}: {str(e)[:120]}")
                        continue
                    state["submit_errors"] += 1
                    log(f"submit error {t['op']}: {e}")
                    if state["submit_errors"] >= 5:
                        raise SystemExit(4)
                break
            if acted:
                continue
            # baseline_paused=true stops new baseline submissions
            # (in-flight jobs still finalize)
            if base_paused:
                break
            base_inflight = {e.get("op") for e in state["inflight"].values()
                             if e["type"] == "baseline"}
            todo = baseline_todo(base_inflight)
            if not todo:
                break
            try:
                submit_baseline(todo[0], state)
                timing_infl += 1
            except Exception as e:  # noqa: BLE001
                state["submit_errors"] += 1
                log(f"baseline submit error {todo[0]['op']}: {e}")
                if state["submit_errors"] >= 5:
                    raise SystemExit(4)
                break
        n_pend, n_infl = len(pend), len(state["inflight"])
        done_base = 230 - len(baseline_todo())
        _cycle_dt = time.time() - _t_cycle
        if _cycle_dt > 60:
            log(f"SLOW-CYCLE {_cycle_dt:.0f}s (watchdog {wd_s}s)")
        log(f"cycle ok: pend={n_pend}(corr {len(corr)}/tim {len(tim)}) "
            f"infl={n_infl} baseline={done_base}/230"
            f"{' BASE_PAUSED' if base_paused else ''}"
            f"{' CORR_PAUSED' if corr_paused else ''} corr_base={base_label(cb)}")
        atomic_write(STATE_FILE, state)
        faulthandler.cancel_dump_traceback_later()
        time.sleep(CYCLE_S)


if __name__ == "__main__":
    main()
